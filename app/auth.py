"""Operator authentication edge (human/operator trust domain).

This guards the LOCAL HTTP API with a bearer token and is deliberately
separate from agent-to-agent Ed25519 crypto (see ``app.identity`` and
``app/a2a``): two trust domains, two mechanisms, never collapsed.

Per-request pipeline::

    HTTP Request → Authentication → Principal → Route → Service

The :class:`AuthenticatedPrincipal` carries the owner/agent resolution
for future multi-agent scoping. Today both resolve to the local
singleton (``owner_id="local"``, ``agent_id="default"``) because one
laptop has one operator by design — but request bodies must NEVER be
trusted for owner/agent identity; only this principal counts.

Open paths: ``GET /health`` (container liveness) and CORS preflights
(``OPTIONS``). Everything else requires
``Authorization: Bearer <operator token>`` (see ``machine_config``
``get_or_create_operator_token``) and fails closed with 401.

Limitation: this middleware only sees HTTP scopes (Starlette's
``BaseHTTPMiddleware`` passes websockets straight through). The
``/ask/live`` bridge therefore enforces its ticket in-route — see
``live_bridge`` in ``app/api/routes/ask.py``.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

OPEN_PATHS = ("/health",)

#: Request-scoped principal for code without HTTP access (services
#: default to the local operator when no request set one — the
#: single-operator deployment behavior, explicit here).
_current_principal: ContextVar["AuthenticatedPrincipal | None"] = (
    ContextVar("nexus_principal", default=None)
)


def current_principal() -> "AuthenticatedPrincipal":
    """Principal for this request, or the default local operator."""
    principal = _current_principal.get()
    if principal is None:
        return AuthenticatedPrincipal(
            owner_id="local", agent_id="default",
            scopes=("operator",), is_operator_token=True)
    return principal

#: Browsers cannot set Authorization headers on WebSocket handshakes, so
#: the live bridge authenticates with a single-use ticket instead: the UI
#: fetches ``POST /ask/live-ticket`` (bearer-authed) then connects to
#: ``/ask/live?ticket=…``. Tickets live 60s, redeem once, and live only
#: in process memory (single-worker dev deployment; a shared store would
#: be needed for multi-worker setups).
_LIVE_TICKET_TTL_SECONDS = 60.0
_live_tickets: dict[str, float] = {}


def issue_live_ticket() -> str:
    """Mint a single-use live-bridge ticket (caller must be authed)."""
    now = time.monotonic()
    expired = [token for token, until in _live_tickets.items() if until <= now]
    for token in expired:
        del _live_tickets[token]
    ticket = secrets.token_urlsafe(24)
    _live_tickets[ticket] = now + _LIVE_TICKET_TTL_SECONDS
    return ticket


def redeem_live_ticket(ticket: str) -> bool:
    """Consume one ticket; False when unknown, expired, or reused."""
    until = _live_tickets.pop(ticket, 0.0)
    return bool(ticket) and until > time.monotonic()


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Verifiably established caller identity for one request."""

    owner_id: str
    agent_id: str
    scopes: tuple[str, ...] = field(default_factory=lambda: ("operator",))
    #: True only when the bearer matched the file/env operator token
    #: (``verify_operator_token``) — never for DB-issued tokens, even
    #: ones carrying a nominal ``operator`` scope. Gates the
    #: token/owner management endpoints via :func:`require_operator`.
    is_operator_token: bool = False

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return credentials.strip()


class OwnerDenied(RuntimeError):
    """Internal: resource belongs to another owner (surfaced as 404)."""


def principal_for_request(request: Request) -> AuthenticatedPrincipal | None:
    """Return the principal stored by the middleware, if any."""
    principal = getattr(request.state, "principal", None)
    return (
        principal if isinstance(principal, AuthenticatedPrincipal) else None
    )


def require_principal(request: Request) -> AuthenticatedPrincipal:
    """Route-level accessor; raises 401-shaped error when unauthenticated."""
    principal = principal_for_request(request)
    if principal is None:
        raise _Unauthorized()
    return principal


class _Unauthorized(RuntimeError):
    """Internal: route asked for a principal the middleware never set."""


class _Forbidden(RuntimeError):
    """Internal: principal lacks operator privilege (surfaced as 403)."""


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "detail": "Operator authentication required.",
            "code": "UNAUTHORIZED",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden_response() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "detail": "Operator privilege required.",
            "code": "FORBIDDEN",
        },
    )


def require_operator(request: Request) -> AuthenticatedPrincipal:
    """Route dependency: the file/env operator principal only.

    DB-issued tokens — even ones carrying a nominal ``operator`` scope —
    cannot mint/revoke tokens, manage owners, or rotate the operator
    token. Only the operator token (file or ``NEXUS_OPERATOR_TOKEN``)
    is trusted for that. Use as ``Depends(require_operator)``.
    """
    principal = require_principal(request)
    if not principal.is_operator_token:
        raise _Forbidden()
    return principal


def client_ip(request: Request) -> str:
    """Best-effort client IP for rate-limit keys.

    ``X-Forwarded-For`` is only trusted behind an explicit
    ``TRUST_PROXY`` env var (``1``/``true``/``yes``/``on``) — otherwise
    the header is trivially spoofable and an attacker can rotate the
    rate-limit bucket at will. Default is the direct peer address.
    """
    if os.environ.get("TRUST_PROXY", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        forwarded = (
            request.headers.get("x-forwarded-for", "") or ""
        ).split(",")[0].strip()
        if forwarded:
            return forwarded
    try:
        if request.client is not None:
            return request.client.host
    except Exception:  # noqa: BLE001 - best-effort only
        logger.debug("client_ip: request.client unreadable", exc_info=True)
    return "unknown"


class OperatorAuthMiddleware(BaseHTTPMiddleware):
    """Enforce bearer-token auth on every non-open path (fail closed)."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.method == "OPTIONS" or request.url.path in OPEN_PATHS:
            return await call_next(request)
        principal = _resolve_principal(_bearer_token(request))
        if principal is None:
            return _unauthorized_response()
        request.state.principal = principal
        reset = _current_principal.set(principal)
        try:
            try:
                return await call_next(request)
            except _Unauthorized:
                return _unauthorized_response()
            except _Forbidden:
                return _forbidden_response()
        finally:
            _current_principal.reset(reset)


def _resolve_principal(token: str) -> AuthenticatedPrincipal | None:
    """File/env token → local operator; else DB token → its owner.

    Unknown, revoked, and disabled-owner tokens all fail closed.
    """
    if not token:
        return None
    from app.machine_config import verify_operator_token

    if verify_operator_token(token):
        return AuthenticatedPrincipal(
            owner_id="local", agent_id="default", scopes=("operator",),
            is_operator_token=True)
    try:
        from app import owners
        from app.store import migrate, open_db
    except ImportError:
        return None
    # sqlite3 errors propagate (→ 500): a corrupt DB must never
    # silently de-authenticate every request. migrate() runs first so a
    # merely-unmigrated DB resolves to 401 (unknown token), not 500.
    path = os.environ.get("NEXUS_DB_PATH", "data/nexus.db")
    conn = open_db(path)
    try:
        migrate(conn)
        resolved = owners.resolve_token(conn, token)
    finally:
        conn.close()
    if resolved is None:
        return None
    return AuthenticatedPrincipal(
        owner_id=resolved["owner_id"], agent_id="default",
        scopes=tuple(resolved["scopes"]) or ("operator",))


def owner_404() -> dict[str, str]:
    """Uniform not-found body for cross-owner denials."""
    return {"detail": "Not found.", "code": "NOT_FOUND"}


__all__ = [
    "OPEN_PATHS",
    "AuthenticatedPrincipal",
    "OperatorAuthMiddleware",
    "OwnerDenied",
    "client_ip",
    "current_principal",
    "issue_live_ticket",
    "owner_404",
    "principal_for_request",
    "redeem_live_ticket",
    "require_operator",
    "require_principal",
]
