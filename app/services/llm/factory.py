"""Env-driven single active LLM provider."""

from __future__ import annotations

from app.config import LLM_OFFLINE_MODE, LLM_PROVIDER
from app.services.llm.base import BaseLLMProvider

_provider_singleton: BaseLLMProvider | None = None


def get_llm_provider() -> BaseLLMProvider:
    """Lazily instantiate the configured provider (avoids importing unused SDKs)."""
    global _provider_singleton
    if _provider_singleton is not None:
        return _provider_singleton

    if LLM_OFFLINE_MODE or LLM_PROVIDER == "offline":
        from app.services.llm.offline_provider import OfflineProvider

        _provider_singleton = OfflineProvider()
        return _provider_singleton

    provider = LLM_PROVIDER.strip().lower()
    if provider in ("anthropic", "claude"):
        from app.services.llm.anthropic_provider import AnthropicProvider

        _provider_singleton = AnthropicProvider()
    elif provider == "openai":
        from app.services.llm.openai_provider import OpenAIProvider

        _provider_singleton = OpenAIProvider()
    elif provider == "groq":
        from app.services.llm.groq_provider import GroqProvider

        _provider_singleton = GroqProvider()
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r} (expected anthropic, openai, groq, offline)")
    return _provider_singleton


def reset_llm_provider() -> None:
    """Clear the singleton — used by tests that switch providers."""
    global _provider_singleton
    _provider_singleton = None
