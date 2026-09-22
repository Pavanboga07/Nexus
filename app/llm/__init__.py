"""LLM package (V5): generic OpenAI-compatible provider."""

from __future__ import annotations

from app.llm.provider import (
    MAX_TOOL_ROUNDS,
    SYSTEM_PROMPT,
    LLMConfig,
    MissingKeyError,
    OpenAICompatibleProvider,
    build_digest,
    escape_retrieved,
    extract_citations,
    get_llm_config,
    honest_empty_message,
    wrap_retrieved,
)

__all__ = [
    "LLMConfig",
    "MAX_TOOL_ROUNDS",
    "SYSTEM_PROMPT",
    "MissingKeyError",
    "OpenAICompatibleProvider",
    "build_digest",
    "escape_retrieved",
    "extract_citations",
    "get_llm_config",
    "honest_empty_message",
    "wrap_retrieved",
]
