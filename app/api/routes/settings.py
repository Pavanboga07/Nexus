"""Model-key settings API: paste the key in the UI, verified live.

``POST /settings/llm-key {key}`` checks the key against the provider
(``GET {base_url}/models``, short timeout) and stores it only on
success — the provider's own error is returned verbatim on failure.
``GET /settings/llm-status`` reports ``{configured, provider_hint}``
and never the value.
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/settings", tags=["settings"])

VERIFY_TIMEOUT = 10.0


class LlmKeyIn(BaseModel):
    key: str = ""


class LlmKeyVerificationError(RuntimeError):
    """Live provider check rejected the key (message is provider-verbatim)."""


def _llm_base_url() -> str:
    return (
        os.environ.get("NEXUS_LLM_BASE_URL", "").strip()
        or "https://api.openai.com/v1"
    ).rstrip("/")


def _llm_model() -> str:
    return os.environ.get("NEXUS_LLM_MODEL", "").strip() or "gpt-4o-mini"


def verify_llm_key(key: str, base_url: str, timeout: float = VERIFY_TIMEOUT) -> None:
    """Live-check ``key`` via ``GET {base_url}/models``. Raises on failure."""
    import httpx

    url = base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
    except Exception as exc:
        raise LlmKeyVerificationError(
            f"provider check failed for {url}: {exc}"
        ) from exc
    if resp.status_code == 200:
        return
    raise LlmKeyVerificationError(
        f"provider rejected the key (HTTP {resp.status_code}): "
        f"{resp.text[:1000]}"
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
    }


@router.post("/llm-key")
def set_llm_key_route(body: LlmKeyIn):
    from app.machine_config import MachineConfigError, set_llm_key

    cleaned = body.key.strip() if isinstance(body.key, str) else ""
    if not cleaned:
        return JSONResponse(
            status_code=400,
            content={"detail": "Model key must not be empty.", "code": "EMPTY_KEY"},
        )
    try:
        verify_llm_key(cleaned, _llm_base_url())
    except LlmKeyVerificationError as exc:
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc), "code": "INVALID_KEY"},
        )
    try:
        set_llm_key(cleaned)
    except (MachineConfigError, ValueError) as exc:
        return JSONResponse(
            status_code=500, content={"detail": str(exc), "code": "CONFIG_CORRUPT"}
        )
    return {"ok": True}


__all__ = ["LlmKeyVerificationError", "router", "verify_llm_key"]
