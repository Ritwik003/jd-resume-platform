"""Shared LLM provider types and errors."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMTransientError(Exception):
    """Retryable: rate limits, timeouts, 5xx."""


class LLMPermanentError(Exception):
    """Do not retry: bad credentials, invalid model id, malformed request."""


@dataclass(slots=True)
class LLMResponse:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class BaseLLMProvider(ABC):
    """Single-vendor chat completion with optional tool calling."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short slug: anthropic, openai, groq, offline."""

    @abstractmethod
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
    ) -> LLMResponse: ...


#: A balanced block. The closing tag is a backreference to the opening one.
#: This line previously held a literal 0x01 byte where the "\1" belonged —
#: written through a shell heredoc, "\1" was expanded into the control
#: character it names. The pattern then matched nothing, and the orphan
#: patterns below silently carried the whole job.
_REASONING_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
#: Reasoning models frequently emit only the CLOSING tag — the opening one is
#: consumed as a separate stream channel and never reaches ``message.content``.
_ORPHAN_CLOSE = re.compile(r"^.*?</(?:think|thinking|reasoning)>", re.S | re.I)
_ORPHAN_OPEN = re.compile(r"<(?:think|thinking|reasoning)>.*$", re.S | re.I)
_JSON_START = re.compile(r"[\{\[]")


def strip_reasoning(text: str) -> str:
    """Remove reasoning spans some models emit around their real answer.

    Reasoning models (MiniMax M2.x, DeepSeek-R1, QwQ and friends) wrap their
    scratchpad in ``<think>`` tags. In practice three shapes arrive, and every
    caller here feeds the result straight into ``json.loads``, so all three have
    to be handled before the text leaves the provider:

    * a balanced ``<think>...</think>`` block,
    * a stray ``</think>`` with no opening tag — the common OpenAI-compatible
      case, where the opening tag never reaches ``message.content``,
    * an unterminated ``<think>`` when the reply hit the token ceiling.
    """
    if not text:
        return ""
    cleaned = _REASONING_BLOCK.sub("", text)
    if "</think" in cleaned.lower() or "</reasoning" in cleaned.lower():
        cleaned = _ORPHAN_CLOSE.sub("", cleaned, count=1)
    if "<think" in cleaned.lower() or "<reasoning" in cleaned.lower():
        # An unterminated block means the reply ran out of budget while still
        # reasoning, so there is no answer to keep. Returning "" is correct, and
        # it is the caller's cue to retry with more room - not a parsing bug,
        # though it looks like one from the outside.
        cleaned = _ORPHAN_OPEN.sub("", cleaned, count=1)
    return cleaned.strip()


def extract_json(text: str) -> str:
    """Return the outermost JSON value in ``text``, or the text unchanged.

    A last line of defence for models that narrate around their JSON despite
    being told not to. Scans for the first ``{``/``[`` and walks to its matching
    close, respecting strings and escapes so a brace inside a value cannot end
    the scan early.
    """
    if not text:
        return text
    m = _JSON_START.search(text)
    if not m:
        return text
    start = m.start()
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text
