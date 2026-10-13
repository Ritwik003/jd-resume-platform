"""Routes deepeval's judge calls through this project's own LLM provider.

deepeval defaults to calling OpenAI directly with its own API key. This
project already picks one of several vendors at runtime via ``LLM_PROVIDER``
(``app/services/llm``), so the eval suite is judged by whichever model the
app itself is actually configured to run on — an Anthropic deployment gets
judged by Anthropic, not by a second, unrelated OpenAI key nobody set up for
evaluation.
"""

from __future__ import annotations

import json
import typing

from deepeval.models.base_model import DeepEvalBaseLLM

from app.config import LLM_MODEL, LLM_TIMEOUT_SECONDS
from app.services.llm.base import extract_json
from app.services.llm.factory import get_llm_provider


class ProjectLLM(DeepEvalBaseLLM):
    """Adapts ``app.services.llm.BaseLLMProvider`` to deepeval's model interface."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model or LLM_MODEL

    def load_model(self) -> ProjectLLM:
        return self

    def generate(self, prompt: str, *args, **kwargs) -> str:
        provider = get_llm_provider()
        response = provider.chat(
            [{"role": "user", "content": prompt}],
            model=self._model,
            temperature=0.0,
            timeout=LLM_TIMEOUT_SECONDS,
        )
        return response.text

    async def a_generate(self, prompt: str, *args, **kwargs) -> str:
        # The provider layer is synchronous SDK calls; there is no async
        # client underneath to actually gain concurrency from here, so async
        # judge calls just run the sync path.
        return self.generate(prompt, *args, **kwargs)

    # ── structured output ────────────────────────────────────────────────
    # deepeval calls a_generate_with_schema(prompt, schema=...) and, when what
    # comes back is not already an instance of that schema, falls through to
    # parsing the raw text and calling schema(**data) itself. That path is
    # brittle against a reasoning model: MiniMax-M3 answers a single-verdict
    # node with {"verdict": ["Partially satisfied"]} -- the right verdict,
    # wrapped in a list -- and deepeval's Literal field rejects it, so the DAG
    # crashes mid-traversal with a pydantic ValidationError instead of
    # returning a score. Building and returning the schema object here takes
    # deepeval's isinstance() fast path and lets us repair that shape first.

    @staticmethod
    def _coerce(data: dict, schema: type) -> dict:
        """Unwrap single-item lists where the schema wants one scalar.

        Only ever narrows [x] -> x, and only for a field the schema does not
        declare as a list, so a genuinely repeated field is left alone and a
        list holding more than one answer is passed through to fail loudly
        rather than being silently truncated to its first element.
        """
        fields = getattr(schema, "model_fields", {})
        out = dict(data)
        for name, value in data.items():
            field = fields.get(name)
            if field is None or not isinstance(value, list) or len(value) != 1:
                continue
            origin = typing.get_origin(field.annotation)
            if origin in (list, set, tuple):
                continue
            out[name] = value[0]
        return out

    def generate_with_schema(self, prompt: str, schema=None, *args, **kwargs):
        text = self.generate(prompt)
        if schema is None:
            return text
        try:
            data = json.loads(extract_json(text))
        except (json.JSONDecodeError, TypeError):
            # Hand the raw text back and let deepeval report the parse failure
            # in its own terms rather than raising a different error here.
            return text
        if not isinstance(data, dict):
            return text
        return schema(**self._coerce(data, schema))

    async def a_generate_with_schema(self, prompt: str, schema=None, *args, **kwargs):
        return self.generate_with_schema(prompt, schema, *args, **kwargs)

    def get_model_name(self) -> str:
        return f"{get_llm_provider().provider_name}:{self._model}"
