"""One coded-error base class for the whole app (finding A1).

Seven modules used to define byte-identical ``*Error(code, message,
status)`` classes. They now share :class:`NexusError`; each module keeps a
thin subclass under its historic name so ``from app.workflows import
WorkflowError`` and ``except WorkflowError`` keep working unchanged —
including ``except ValueError`` for ``PairingError``, which stays a
``ValueError`` via multiple inheritance.
"""

from __future__ import annotations


class NexusError(RuntimeError):
    """Coded application error: machine ``code`` + HTTP ``status``.

    The string form is ``"CODE: message"`` so a bare ``str(exc)`` stays
    readable in logs; route layers translate ``code``/``status`` into the
    JSON error envelope via :func:`app.api.errors.coded_error_response`.
    """

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status
