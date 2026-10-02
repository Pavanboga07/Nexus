"""Cross-thread SQLite use (generator-dependency thread hop).

Proven production crash: ``sqlite3.ProgrammingError: SQLite objects
created in a thread can only be used in that same thread`` from pairing
routes. Route dependencies are generator-based (``yield conn`` + close
in finally), and FastAPI/anyio may run setup, route body, and teardown
on DIFFERENT worker threads — default ``check_same_thread=True`` then
explodes. ``open_db`` must open with ``check_same_thread=False``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


def test_open_db_conn_usable_across_threads(tmp_path):
    from app.store import migrate, open_db

    conn = open_db(str(tmp_path / "threads.db"))
    migrate(conn)

    def use_from_worker():
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_version"
        ).fetchone()
        assert int(row["v"]) >= 1
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (999,),
            )
        conn.close()
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(use_from_worker).result(timeout=30) is True
