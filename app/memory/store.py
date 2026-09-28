"""SQLite + sqlite-vec memory store (V6).

Schema (per-install, single owner — no tenant code):

- ``memories`` — ``id TEXT PK, text, session_id, created_at`` (+ rowid
  shared with the two index tables below)
- ``memory_vec`` — ``vec0(embedding float[64])`` virtual table;
  ``memory_vec.rowid == memories.rowid``
- ``memory_fts`` — ``fts5(text)`` companion; ``memory_fts.rowid ==
  memories.rowid``
- ``memory_audit`` — append-only ``(memory_id, action, at)`` log so a
  forget leaves a record that something was removed (never the text).

HYBRID RECALL BLEND (explicit, as planned)::

    V = top vec_k memories by vector L2 distance (sqlite-vec)
    F = top fts_k memories by FTS5 bm25 rank (keyword match)
    R = union(V, F), deduped by id, ordered:
        1. in BOTH V and F (by vector distance)
        2. vector-only (by vector distance)
        3. FTS-only (by bm25 rank)
    return R[:limit]

Rationale: semantic-ish similarity leads, exact keywords rescue
paraphrase misses, and items both legs agree on rank first.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from app.memory.embeddings import DIM, embed, tokenize

VEC_K_DEFAULT = 10
FTS_K_DEFAULT = 10


def _load_vec(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())


def _fts_query(query: str) -> str | None:
    tokens = tokenize(query)
    if not tokens:
        return None
    return " OR ".join('"' + t.replace('"', "") + '"' for t in tokens)


class MemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        parent = str(Path(self.path).parent)
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        _load_vec(self._conn)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        c = self._conn
        with c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT ''
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS memory_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    at TEXT NOT NULL DEFAULT ''
                )"""
            )
            c.execute(
                f"""CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec
                    USING vec0(embedding float[{DIM}])"""
            )
            c.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                    USING fts5(text)"""
            )

    # SECTION: write path

    def add(
        self,
        text: str,
        session_id: str = "",
        created_at: str = "",
        memory_id: str | None = None,
    ) -> str:
        mid = memory_id or uuid.uuid4().hex[:16]
        with self._conn:
            row = self._conn.execute(
                "SELECT rowid FROM memories WHERE id = ?", (mid,)
            ).fetchone()
            if row is not None:
                return mid
            cur = self._conn.execute(
                "INSERT INTO memories (id, text, session_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (mid, text, session_id, created_at),
            )
            rowid = cur.lastrowid
            self._write_index(rowid, text)
        return mid

    def _write_index(self, rowid: int, text: str) -> None:
        import sqlite_vec

        blob = sqlite_vec.serialize_float32(embed(text))
        self._conn.execute(
            "INSERT INTO memory_vec(rowid, embedding) VALUES (?, ?)",
            (rowid, blob),
        )
        self._conn.execute(
            "INSERT INTO memory_fts(rowid, text) VALUES (?, ?)",
            (rowid, text),
        )

    def add_many(
        self, texts: list[str], session_id: str = "", created_at: str = ""
    ) -> list[str]:
        return [
            self.add(t, session_id=session_id, created_at=created_at)
            for t in texts
        ]

    def forget(self, memory_id: str) -> bool:
        row = self._conn.execute(
            "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return False
        rowid = row["rowid"]
        with self._conn:
            self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.execute("DELETE FROM memory_vec WHERE rowid = ?", (rowid,))
            self._conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (rowid,))
            self._conn.execute(
                "INSERT INTO memory_audit (memory_id, action) VALUES (?, 'forget')",
                (memory_id,),
            )
        return True

    # SECTION: read path (hybrid recall)

    def recall(
        self,
        query: str,
        limit: int = 5,
        vec_k: int = VEC_K_DEFAULT,
        fts_k: int = FTS_K_DEFAULT,
        now: str | None = None,  # frozen clock hook for tests; ranking is time-free in v1
        session_id: str | None = None,  # None = all sessions (browser); "" or an id scopes
    ) -> list[dict]:
        import sqlite_vec

        _ = now  # reserved: recency weighting is a post-v1 tuning knob
        qblob = sqlite_vec.serialize_float32(embed(query))
        vec_k = max(1, int(vec_k))
        fts_k = max(1, int(fts_k))
        scope = (
            " AND rowid IN (SELECT rowid FROM memories WHERE session_id = ?)"
            if session_id is not None
            else ""
        )
        vec_params: tuple = (
            (qblob, session_id) if session_id is not None else (qblob,)
        )
        vec_rows = self._conn.execute(
            "SELECT rowid, distance FROM memory_vec"
            f" WHERE embedding MATCH ? AND k = {vec_k}{scope} ORDER BY distance",
            vec_params,
        ).fetchall()
        vec_order = {r["rowid"]: r["distance"] for r in vec_rows}
        fts_rows: list = []
        match = _fts_query(query)
        if match is not None:
            try:
                fts_params: tuple = (
                    (match, session_id, fts_k)
                    if session_id is not None
                    else (match, fts_k)
                )
                fts_rows = self._conn.execute(
                    "SELECT rowid, rank FROM memory_fts"
                    f" WHERE memory_fts MATCH ?{scope}"
                    " ORDER BY rank LIMIT ?",
                    fts_params,
                ).fetchall()
            except sqlite3.OperationalError:
                fts_rows = []
        fts_rank = {r["rowid"]: r["rank"] for r in fts_rows}

        def _key(rowid: int) -> tuple:
            in_both = rowid in vec_order and rowid in fts_rank
            only_vec = rowid in vec_order
            if in_both:
                return (0, vec_order[rowid])
            if only_vec:
                return (1, vec_order[rowid])
            return (2, fts_rank[rowid])

        ordered = sorted(set(vec_order) | set(fts_rank), key=_key)[:limit]
        out = []
        for r in ordered:
            hit = self._by_rowid(r)
            if hit is not None:
                out.append(hit)
        return out

    def _by_rowid(self, rowid: int) -> dict | None:
        row = self._conn.execute(
            "SELECT id, text, session_id, created_at FROM memories"
            " WHERE rowid = ?",
            (rowid,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "text": row["text"],
            "session_id": row["session_id"],
            "created_at": row["created_at"],
        }

    def list_all(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, text, session_id, created_at FROM memories"
            " ORDER BY rowid"
        ).fetchall()
        return [dict(r) for r in rows]

    def audit_log(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT memory_id, action, at FROM memory_audit ORDER BY audit_id"
        ).fetchall()
        return [dict(r) for r in rows]

    # SECTION: portability

    def export_json(self, path: str | Path) -> int:
        rows = self.list_all()
        Path(path).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        return len(rows)

    def import_json(self, path: str | Path) -> int:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("memory import expects a JSON list")
        count = 0
        for item in payload:
            if not isinstance(item, dict) or "text" not in item:
                raise ValueError("memory import item needs at least 'text'")
            self.add(
                item["text"],
                session_id=item.get("session_id", ""),
                created_at=item.get("created_at", ""),
                memory_id=item.get("id"),
            )
            count += 1
        return count

    def close(self) -> None:
        self._conn.close()


__all__ = ["FTS_K_DEFAULT", "MemoryStore", "VEC_K_DEFAULT"]
