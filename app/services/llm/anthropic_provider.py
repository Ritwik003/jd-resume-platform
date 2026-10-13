"""Anthropic Claude provider."""

from __future__ import annotations

from typing import Any

from app.config import ANTHROPIC_API_KEY, LLM_MAX_TOKENS
from app.services.llm.base import (
    BaseLLMProvider,
    LLMPermanentError,
    LLMResponse,
    LLMTransientError,
    strip_reasoning,
)


class AnthropicProvider(BaseLLMProvider):
    def __init__(self) -> None:
        if not ANTHROPIC_API_KEY:
            raise LLMPermanentError("ANTHROPIC_API_KEY is not configured")
        import anthropic

        self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self._sdk = anthropic

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        timeout: float = 60.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            # Anthropic requires a ceiling, so a caller that names none gets
            # the platform floor rather than a hard-coded 4096 that would
            # quietly undercut every budget set elsewhere.
            "max_tokens": max_tokens or LLM_MAX_TOKENS,
            "temperature": temperature,
            "messages": messages,
            "timeout": timeout,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
                }
                for t in tools
            ]
        try:
            resp = self._client.messages.create(**kwargs)
        except (self._sdk.RateLimitError, self._sdk.APITimeoutError, self._sdk.InternalServerError) as exc:
            raise LLMTransientError(str(exc)) from exc
        except self._sdk.APIStatusError as exc:
            if exc.status_code and exc.status_code >= 500:
                raise LLMTransientError(str(exc)) from exc
            raise LLMPermanentError(str(exc)) from exc

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "arguments": dict(block.input or {})})

        return LLMResponse(
            text=strip_reasoning("\n".join(text_parts)),
            tool_calls=tool_calls,
            model=resp.model,
            prompt_tokens=getattr(resp.usage, "input_tokens", None),
            completion_tokens=getattr(resp.usage, "output_tokens", None),
        )
