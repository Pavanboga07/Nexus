"""Model-key settings API: paste the key in the UI, verified live.

``POST /settings/llm-key {key, base_url?, model?}`` checks the key
against the provider (``GET {base_url}/models``, short timeout) and
stores key + provider + model only on success — the provider's own
error is returned verbatim on failure. Omitting the key stores just
the provider/model switch (verification is deferred to the next key
save or chat turn). ``GET /settings/llm-status`` reports
``{configured, provider_hint, base_url, model}`` and never the value.
``GET /settings/llm-models`` lists the current provider's model ids.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import (
    AuthenticatedPrincipal,
    client_ip,
    require_operator,
)

router = APIRouter(prefix="/settings", tags=["settings"])

VERIFY_TIMEOUT = 10.0


class LlmKeyIn(BaseModel):
    key: str = ""
    base_url: str = ""
    model: str = ""


class LlmKeyVerificationError(RuntimeError):
    """Live provider check rejected the key (message is provider-verbatim)."""


def _llm_base_url() -> str:
    from app.machine_config import resolve_llm_base_url

    return resolve_llm_base_url()


def _llm_model() -> str:
    from app.machine_config import resolve_llm_model

    return resolve_llm_model()


def _models_request(key: str, base_url: str, timeout: float):
    """GET ``{base_url}/models`` with the key; returns the raw response."""
    import httpx

    url = base_url.rstrip("/") + "/models"
    try:
        return httpx.get(
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
    except Exception as exc:
        raise LlmKeyVerificationError(
            f"provider check failed for {url}: {exc}"
        ) from exc


def verify_llm_key(key: str, base_url: str, timeout: float = VERIFY_TIMEOUT) -> None:
    """Live-check ``key`` via ``GET {base_url}/models``. Raises on failure."""
    resp = _models_request(key, base_url, timeout)
    if resp.status_code == 200:
        return
    raise LlmKeyVerificationError(
        f"provider rejected the key (HTTP {resp.status_code}): "
        f"{resp.text[:1000]}"
    )


def list_llm_models(
    key: str, base_url: str, timeout: float = VERIFY_TIMEOUT
) -> list[str]:
    """Model ids visible to ``key`` at ``GET {base_url}/models``."""
    resp = _models_request(key, base_url, timeout)
    if resp.status_code != 200:
        raise LlmKeyVerificationError(
            f"provider rejected the key (HTTP {resp.status_code}): "
            f"{resp.text[:1000]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise LlmKeyVerificationError(
            f"provider returned non-JSON model list: {exc}"
        ) from exc
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return sorted(
        {
            str(entry.get("id", "")).strip()
            for entry in items
            if isinstance(entry, dict) and str(entry.get("id", "")).strip()
        }
    )


@router.get("/llm-status")
def llm_status():
    from app.machine_config import MachineConfigError, resolve_llm_key

    try:
        configured = resolve_llm_key() is not None
    except MachineConfigError as exc:
        return JSONResponse(
            status_code=500, content={"detail": str(exc), "code": "CONFIG_CORRUPT"}
        )
    return {
        "configured": configured,
        "provider_hint": f"{_llm_model()} @ {_llm_base_url()}",
        "base_url": _llm_base_url(),
        "model": _llm_model(),
    }


@router.post("/llm-key")
def set_llm_key_route(body: LlmKeyIn,
                      _operator: AuthenticatedPrincipal = Depends(require_operator)):
    from app.machine_config import (
        MachineConfigError,
        set_llm_base_url,
        set_llm_key,
        set_llm_model,
    )

    cleaned = body.key.strip() if isinstance(body.key, str) else ""
    base_url = body.base_url.strip().rstrip("/") if isinstance(body.base_url, str) else ""
    model = body.model.strip() if isinstance(body.model, str) else ""
    if not cleaned and not base_url and not model:
        return JSONResponse(
            status_code=400,
            content={"detail": "Model key must not be empty.", "code": "EMPTY_KEY"},
        )
    try:
        if cleaned:
            # Verify against the submitted provider (or the current one
            # when only a key arrives, preserving the old contract).
            verify_llm_key(cleaned, base_url or _llm_base_url())
            set_llm_key(cleaned)
        if base_url:
            set_llm_base_url(base_url)
        if model:
            set_llm_model(model)
    except LlmKeyVerificationError as exc:
        # Nothing is stored when verification fails.
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "INVALID_KEY"},
        )
    except (MachineConfigError, ValueError) as exc:
        return JSONResponse(
            status_code=500, content={"detail": str(exc), "code": "CONFIG_CORRUPT"}
        )
    return {"ok": True}


@router.get("/llm-models")
def llm_models_route():
    from app.machine_config import MachineConfigError, resolve_llm_key

    try:
        key = resolve_llm_key()
    except MachineConfigError as exc:
        return JSONResponse(
            status_code=500, content={"detail": str(exc), "code": "CONFIG_CORRUPT"}
        )
    if not key:
        return JSONResponse(
            status_code=400,
            content={"detail": "No model key saved yet.", "code": "NO_KEY"},
        )
    try:
        return {"models": list_llm_models(key, _llm_base_url())}
    except LlmKeyVerificationError as exc:
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "INVALID_KEY"},
        )


class OperatorTokenPinnedError(RuntimeError):
    """Rotation refused while the token is pinned by environment."""


class OwnerIn(BaseModel):
    owner_id: str = ""
    display_name: str = ""


def _owner_db():
    from app.agent.context import db_path
    from app.store import migrate, open_db

    conn = open_db(db_path())
    migrate(conn)
    return conn


@router.get("/owners")
def list_owners_route(
    _operator: AuthenticatedPrincipal = Depends(require_operator),
):
    from app import owners
    from app.agent.context import db_path
    from app.store import migrate, open_db

    conn = open_db(db_path())
    migrate(conn)
    try:
        return {"owners": owners.list_owners(conn)}
    finally:
        conn.close()


@router.post("/owners")
def create_owner_route(
    body: OwnerIn,
    _operator: AuthenticatedPrincipal = Depends(require_operator),
):
    from app import owners
    from app.agent.context import db_path
    from app.store import migrate, open_db

    conn = open_db(db_path())
    migrate(conn)
    try:
        return owners.create_owner(
            conn, body.owner_id, body.display_name)
    except owners.OwnerError as exc:
        return JSONResponse(
            status_code=exc.status or 400,
            content={"detail": str(exc), "code": exc.code},
        )
    finally:
        conn.close()


@router.post("/owners/{owner_id}/tokens")
def issue_token_route(
    owner_id: str,
    _operator: AuthenticatedPrincipal = Depends(require_operator),
):
    from app import owners
    from app.agent.context import db_path
    from app.store import migrate, open_db

    conn = open_db(db_path())
    migrate(conn)
    try:
        return owners.issue_token(conn, owner_id)
    except owners.OwnerError as exc:
        return JSONResponse(
            status_code=exc.status or 400,
            content={"detail": str(exc), "code": exc.code},
        )
    finally:
        conn.close()


@router.get("/owners/{owner_id}/tokens")
def list_tokens_route(
    owner_id: str,
    _operator: AuthenticatedPrincipal = Depends(require_operator),
):
    from app import owners
    from app.agent.context import db_path
    from app.store import migrate, open_db

    conn = open_db(db_path())
    migrate(conn)
    try:
        return {"tokens": owners.list_tokens(conn, owner_id)}
    except owners.OwnerError as exc:
        return JSONResponse(
            status_code=exc.status or 400,
            content={"detail": str(exc), "code": exc.code},
        )
    finally:
        conn.close()


@router.post("/tokens/{token_id}/revoke")
def revoke_token_route(
    token_id: str,
    _operator: AuthenticatedPrincipal = Depends(require_operator),
):
    from app import owners
    from app.agent.context import db_path
    from app.store import migrate, open_db

    conn = open_db(db_path())
    migrate(conn)
    try:
        return owners.revoke_token(conn, token_id)
    except owners.OwnerError as exc:
        return JSONResponse(
            status_code=exc.status or 400,
            content={"detail": str(exc), "code": exc.code},
        )
    finally:
        conn.close()


@router.post("/operator-token/rotate")
def rotate_operator_token_route(
    request: Request,
    _operator: AuthenticatedPrincipal = Depends(require_operator),
):
    """Rotate the file operator token; the response carries the new token
    exactly once. Refuses with 409 while ``NEXUS_OPERATOR_TOKEN`` pins it.

    MVP rate limit: 5 rotations per 5 minutes per client (brute-force /
    token-churn protection without Redis).
    """
    from app.machine_config import MachineConfigError, rotate_operator_token
    from app.ratelimit import check_rate_limit

    client = client_ip(request)
    allowed, retry = check_rate_limit(f"rotate:{client}", limit=5, window_seconds=300)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many rotation attempts. Try again later.", "code": "RATE_LIMITED"},
            headers={"Retry-After": str(retry)},
        )
    try:
        return {"token": rotate_operator_token(), "rotated": True}
    except MachineConfigError as exc:
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "code": "PINNED"},
        )


__all__ = [
    "LlmKeyVerificationError",
    "OperatorTokenPinnedError",
    "list_llm_models",
    "router",
    "verify_llm_key",
]
