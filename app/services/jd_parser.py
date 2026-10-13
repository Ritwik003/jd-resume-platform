"""Section 5 / 14 — AI-assisted JD parsing and technology resolution."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import (
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
)
from app.models.technology import Technology
from app.prompts.templates import JD_PARSER_SYSTEM, JD_PARSER_USER
from app.schemas.requirements import ParsedJD, ParsedSkill
from app.services.llm.base import LLMPermanentError, LLMTransientError, extract_json
from app.services.llm.factory import get_llm_provider
from app.services.llm.retry import with_retry
from app.utils.enums import SkillImportance

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text or "").strip()


@dataclass(slots=True)
class JDParseResult:
    parsed: ParsedJD
    resolved: list[ParsedSkill]
    unmatched: list[str]
    model: str | None


def parse_jd_text(raw_jd_text: str) -> tuple[ParsedJD, str | None]:
    """Call the LLM and return a validated :class:`ParsedJD`."""
    provider = get_llm_provider()

    def _call():
        return provider.chat(
            [{"role": "user", "content": JD_PARSER_USER.format(raw_jd_text=raw_jd_text)}],
            model=LLM_MODEL,
            system=JD_PARSER_SYSTEM,
            temperature=LLM_TEMPERATURE,
            # Reasoning models spend most of their budget before the JSON,
            # which is the argument for a LARGER ceiling, not a smaller one.
            # This asked for 4000 flat and ignored the platform floor, making
            # the parse that every downstream decision is built on the most
            # starved call in the system.
            max_tokens=max(LLM_MAX_TOKENS, 16000),
            timeout=LLM_TIMEOUT_SECONDS,
        )

    try:
        response = with_retry(_call, max_retries=LLM_MAX_RETRIES)
    except (LLMTransientError, LLMPermanentError) as exc:
        logger.warning("JD parse failed, returning empty parse: %s", exc)
        return ParsedJD(), None

    try:
        payload = json.loads(extract_json(_strip_fences(response.text)))
    except json.JSONDecodeError:
        logger.warning("JD parser returned non-JSON output")
        return ParsedJD(), response.model

    for key in ("responsibilities", "other_requirements", "mandatory_skills", "preferred_skills"):
        value = payload.get(key)
        if not isinstance(value, list):
            payload[key] = []

    return ParsedJD.model_validate(payload), response.model


def resolve_skills(db: Session, parsed: ParsedJD) -> tuple[list[ParsedSkill], list[str]]:
    """Map raw JD skill labels onto the technology master (section 12 aliases).

    Importance is attached here, on the requirement — never on the technology
    master itself (section 14).
    """
    technologies = db.query(Technology).filter(Technology.is_active.is_(True)).all()

    lookup: dict[str, Technology] = {}
    for tech in technologies:
        lookup[tech.name.strip().lower()] = tech
        for alias in tech.aliases or []:
            lookup.setdefault(str(alias).strip().lower(), tech)

    resolved: list[ParsedSkill] = []
    unmatched: list[str] = []
    seen: set[str] = set()

    for labels, importance in (
        (parsed.mandatory_skills, SkillImportance.MANDATORY),
        (parsed.preferred_skills, SkillImportance.PREFERRED),
    ):
        for raw in labels:
            key = str(raw).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            tech = lookup.get(key)
            if tech is None:
                unmatched.append(str(raw).strip())
                resolved.append(ParsedSkill(raw_label=str(raw).strip(), importance=importance))
            else:
                resolved.append(
                    ParsedSkill(
                        raw_label=str(raw).strip(),
                        technology_id=tech.id,
                        matched_name=tech.name,
                        importance=importance,
                    )
                )
    return resolved, unmatched


def parse_and_resolve(db: Session, raw_jd_text: str) -> JDParseResult:
    parsed, model = parse_jd_text(raw_jd_text)
    resolved, unmatched = resolve_skills(db, parsed)
    return JDParseResult(parsed=parsed, resolved=resolved, unmatched=unmatched, model=model)
