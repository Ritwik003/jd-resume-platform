"""Groq provider (OpenAI-compatible chat completions)."""

from __future__ import annotations

import json
from typing import Any

from app.config import GROQ_API_KEY
from app.services.llm.base import (
    BaseLLMProvider,
    LLMPermanentError,
    LLMResponse,
    LLMTransientError,
    strip_reasoning,
)


class GroqProvider(BaseLLMProvider):
    def __init__(self) -> None:
        if not GROQ_API_KEY:
            raise LLMPermanentError("GROQ_API_KEY is not configured")
        from groq import Groq

        self._client = Groq(api_key=GROQ_API_KEY)

    @property
    def provider_name(self) -> str:
        return "groq"

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
        payload: list[dict[str, Any]] = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend({"role": m["role"], "content": m["content"]} for m in messages)

        kwargs: dict[str, Any] = {"model": model, "messages": payload, "temperature": temperature}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                    },
                }
                for t in tools
            ]
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if any(tok in message.lower() for tok in ("rate limit", "timeout", "503", "502", "500")):
                raise LLMTransientError(message) from exc
            raise LLMPermanentError(message) from exc

        choice = resp.choices[0].message
        tool_calls: list[dict[str, Any]] = []
        for call in getattr(choice, "tool_calls", None) or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": call.id, "name": call.function.name, "arguments": args})

        return LLMResponse(text=strip_reasoning(choice.content or ""), tool_calls=tool_calls, model=model)
