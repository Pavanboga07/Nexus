"""Fact extraction + fire-and-forget background hook (V6).

``extract_facts`` is a deliberately small v1 heuristic: it keeps
sentences that look like durable facts (``remember …``, ``my X is …``,
or any long-enough sentence) and drops chit-chat. A model-based
extractor can replace it without touching the store or the chat hook.

``record_turn`` / ``queue_extraction`` run extraction off the chat
critical path: failures are swallowed (logged nowhere in v1 — a
counter lands with metrics) so a broken extractor can never break a
chat turn.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_REMEMBER = re.compile(r"\bremember\b|\bmy\b.*\bis\b|\bmy\b.*\bare\b", re.I)


def extract_facts(
    user_text: str, assistant_text: str = "", max_facts: int = 5
) -> list[str]:
    candidates = _SENTENCE.findall(user_text or "")
    facts: list[str] = []
    for raw in candidates:
        sentence = " ".join(raw.split())
        if len(sentence.split()) < 4:
            continue
        if _REMEMBER.search(sentence) or len(sentence.split()) >= 7:
            facts.append(sentence)
        if len(facts) >= max_facts:
            break
    if not facts and (user_text or "").strip():
        words = (user_text or "").split()
        if len(words) >= 4:
            facts.append(" ".join(words[:50]))
    return facts[:max_facts]


Extractor = Callable[[str, str], list[str]]


def _default_extractor(user_text: str, assistant_text: str) -> list[str]:
    return extract_facts(user_text, assistant_text)


def record_turn(
    db_path: str | Path,
    user_text: str,
    assistant_text: str = "",
    session_id: str = "",
    created_at: str = "",
    background: bool = True,
    extractor: Extractor | None = None,
    agent_id: str = "",
) -> threading.Thread:
    """Store extracted facts; always returns the worker thread.

    ``background=True`` (default) does the work on a daemon thread so
    callers never wait; ``background=False`` still returns a thread
    but runs inline first (handy in tests). Never raises: extraction
    failures are isolated from chat.
    """
    extract = extractor or _default_extractor
    owner = agent_id or ""
    if background:
        thread = threading.Thread(
            target=_run,
            args=(str(db_path), user_text, assistant_text, session_id,
                  created_at, extract, owner),
            daemon=True,
        )
        thread.start()
        return thread
    _run(str(db_path), user_text, assistant_text, session_id, created_at,
         extract, owner)
    done = threading.Thread(target=lambda: None, daemon=True)
    done.start()
    return done


def queue_extraction(
    db_path: str | Path,
    user_text: str,
    assistant_text: str = "",
    session_id: str = "",
    created_at: str = "",
    extractor: Extractor | None = None,
    agent_id: str = "",
) -> threading.Thread:
    """Alias for ``record_turn(background=True)`` — the chat hook entry."""
    return record_turn(
        db_path, user_text, assistant_text, session_id, created_at,
        background=True, extractor=extractor, agent_id=agent_id,
    )


def _run(db_path, user_text, assistant_text, session_id, created_at,
         extract, agent_id="") -> None:
    import logging

    log = logging.getLogger("nexus.extract")
    try:
        facts = extract(user_text, assistant_text) or []
    except Exception as exc:
        log.warning("extraction failed: %s", type(exc).__name__)
        return
    if not facts:
        return
    try:
        from app.memory.store import MemoryStore

        store = MemoryStore(db_path)
        try:
            store.add_many(facts, session_id=session_id,
                             created_at=created_at,
                             agent_id=agent_id or "default")
        finally:
            store.close()
    except Exception as exc:
        log.warning("extraction store failed: %s", type(exc).__name__)
        return


__all__ = ["extract_facts", "queue_extraction", "record_turn"]
