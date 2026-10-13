"""OpenAI / OpenAI-compatible provider (section 37: "OpenAI-compatible model")."""

from __future__ import annotations

import json
from typing import Any

from app.config import OPENAI_API_KEY, OPENAI_BASE_URL
from app.services.llm.base import (
    BaseLLMProvider,
    LLMPermanentError,
    LLMResponse,
    LLMTransientError,
    strip_reasoning,
)


class OpenAIProvider(BaseLLMProvider):
    def __init__(self) -> None:
        if not OPENAI_API_KEY:
            raise LLMPermanentError("OPENAI_API_KEY is not configured")
        from openai import OpenAI

        kwargs: dict[str, Any] = {"api_key": OPENAI_API_KEY}
        if OPENAI_BASE_URL:
            kwargs["base_url"] = OPENAI_BASE_URL
        self._client = OpenAI(**kwargs)

    @property
    def provider_name(self) -> str:
        return "openai"

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
        payload_messages: list[dict[str, Any]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
            payload_messages.append({"role": m["role"], "content": content})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": payload_messages,
            "temperature": temperature,
            "timeout": timeout,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
                for t in tools
            ]

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 — SDK exception surface varies
            message = str(exc)
            if any(tok in message.lower() for tok in ("rate limit", "timeout", "503", "502", "500", "overloaded")):
                raise LLMTransientError(message) from exc
            raise LLMPermanentError(message) from exc

        choice = resp.choices[0].message
        tool_calls: list[dict[str, Any]] = []
        for call in choice.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": call.id, "name": call.function.name, "arguments": args})

        return LLMResponse(
            text=strip_reasoning(choice.content or ""),
            tool_calls=tool_calls,
            model=resp.model,
            prompt_tokens=getattr(resp.usage, "prompt_tokens", None),
            completion_tokens=getattr(resp.usage, "completion_tokens", None),
        )
