"""Shared coded-error -> JSON translation for route modules (finding A2).

Seven route modules defined byte-identical ``_error(exc)`` helpers. This
is the single copy; route modules should delegate::

    from app.api.errors import coded_error_response

    @router.exception_handler(...)  # or: except XError as exc:
    def _error(exc):                 #     return coded_error_response(exc)
        return coded_error_response(exc)
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from app.errors import NexusError


def coded_error_response(exc: NexusError) -> JSONResponse:
    """Translate a coded domain error into the standard error envelope.

    ``status or 400`` with ``{"detail": str(exc), "code": exc.code}`` —
    the exact shape every route-local ``_error`` helper produced. Works
    for any coded error exposing ``.code``/``.status`` (including
    ``app.a2a.service.A2AError``, which is not a ``NexusError``).
    """
    return JSONResponse(
        status_code=exc.status or 400,
        content={"detail": str(exc), "code": exc.code},
    )
