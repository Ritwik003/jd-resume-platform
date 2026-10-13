"""The summary is written, then checked — the checks are the interesting part.

Two failure modes matter and both read perfectly: a technology the candidate
does not have, and a paragraph of adjective with no work in it. Neither raises
anything, so neither is caught unless it is looked for.
"""

from __future__ import annotations

import os

os.environ.setdefault("LLM_OFFLINE_MODE", "true")

from app.services import summary_writer as sw  # noqa: E402

GOOD = (
    "Senior AI engineer with seven years building production AI systems on Microsoft "
    "Azure, specialising in RAG retrieval patterns and MCP-coordinated agent workflows. "
    "Built a reusable enterprise AI SDK pairing MCP-coordinated agents with RAG pipelines "
    "over Snowflake and PostgreSQL, cutting per-record processing time. Hands-on across "
    "Azure AI Foundry, LangGraph and FastAPI."
)


def test_a_good_summary_passes():
    assert sw._rejects(GOOD, ["Microsoft Copilot Studio", "Microsoft Fabric"]) is None


def test_a_skill_the_candidate_lacks_is_refused():
    """The fastest way to fail a screen, and it reads perfectly on the page."""
    claimed = GOOD + " Also delivers with Microsoft Copilot Studio."
    reason = sw._rejects(claimed, ["Microsoft Copilot Studio"])
    assert reason and "Copilot Studio" in reason


def test_a_short_skill_name_does_not_match_inside_a_word():
    """"Go" must not be found in "going", or every summary is rejected."""
    assert sw._rejects(GOOD + " The work was going well.", ["Go"]) is None
    # Two characters or fewer are not matched at all — too many false hits.
    assert sw._rejects(GOOD + " Used R for analysis.", ["R"]) is None


def test_filler_adjectives_are_refused():
    for filler in (
        "Passionate engineer who ships.",
        "A seasoned professional.",
        "Results-driven and detail-oriented.",
        "Brings a proven track record.",
    ):
        reason = sw._rejects(GOOD + " " + filler, [])
        assert reason and "filler" in reason, filler


def test_the_first_person_is_refused():
    assert sw._rejects("I built AI systems. " + GOOD, []) is not None
    # A word merely containing "i" or "me" is not the first person.
    assert sw._rejects(GOOD + " Improved metrics in the pipeline.", []) is None


def test_length_is_bounded_at_both_ends():
    assert sw._rejects("Engineer.", []) is not None
    assert sw._rejects("word " * 400, []) is not None


def test_the_wrappers_a_model_adds_are_stripped():
    assert sw._clean('"' + GOOD + '"') == GOOD
    assert sw._clean("Professional Summary: " + GOOD) == GOOD
    assert sw._clean("```\n" + GOOD + "\n```") == GOOD
    assert sw._clean("  Senior   AI   engineer.  ") == "Senior AI engineer."


def test_a_rejected_summary_falls_back_rather_than_raising(monkeypatch):
    """A summary is not worth failing a composition over.

    The templates can always assemble a true one, so an unreachable provider
    or an unusable reply costs a duller paragraph, not the resume.
    """
    from app.schemas.resume_content import ResumeContent

    def boom(*args, **kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(sw, "get_llm_provider", boom)
    summary, model = sw.write(ResumeContent(), role="AI Engineer", years=7.0)
    assert summary is None and model is None


# ────────────────────────────────────────────── the operator's own instruction
def test_a_direction_reaches_the_prompt(monkeypatch):
    """What the operator typed has to arrive verbatim, or it cannot be followed."""
    from app.schemas.resume_content import ResumeContent

    seen: dict[str, str] = {}

    class _Provider:
        def chat(self, messages, **kwargs):
            seen["user"] = messages[0]["content"]
            seen["system"] = kwargs.get("system", "")

            class _R:
                text = GOOD
                model = "test"

            return _R()

    monkeypatch.setattr(sw, "get_llm_provider", lambda: _Provider())
    summary, model = sw.write(
        ResumeContent(),
        role="AI Engineer",
        years=7.0,
        direction="Lead with the platform work, less about the models.",
    )
    assert summary == GOOD and model == "test"
    assert "Lead with the platform work, less about the models." in seen["user"]
    assert "WHAT THE OPERATOR ASKED FOR" in seen["user"]
    # And the system prompt has to say the instruction outranks the defaults,
    # or the model weighs a house rule against the person who typed it.
    assert "DIRECTION, IT WINS" in seen["system"]


def test_no_direction_leaves_no_empty_heading(monkeypatch):
    from app.schemas.resume_content import ResumeContent

    seen: dict[str, str] = {}

    class _Provider:
        def chat(self, messages, **kwargs):
            seen["user"] = messages[0]["content"]

            class _R:
                text = GOOD
                model = "test"

            return _R()

    monkeypatch.setattr(sw, "get_llm_provider", lambda: _Provider())
    sw.write(ResumeContent(), role="AI Engineer", years=7.0)
    assert "WHAT THE OPERATOR ASKED FOR" not in seen["user"]


def test_a_direction_cannot_talk_a_forbidden_skill_onto_the_page(monkeypatch):
    """"Mention Copilot Studio prominently" must not defeat the missing-skill guard.

    The instruction outranks the house style, never the grounding — so a
    summary that claims it is rejected however it was asked for.
    """
    from app.schemas.resume_content import ResumeContent

    class _Provider:
        def chat(self, messages, **kwargs):
            class _R:
                text = GOOD + " Delivers with Microsoft Copilot Studio."
                model = "test"

            return _R()

    monkeypatch.setattr(sw, "get_llm_provider", lambda: _Provider())
    summary, _ = sw.write(
        ResumeContent(),
        role="AI Engineer",
        years=7.0,
        missing_skills=["Microsoft Copilot Studio"],
        direction="Mention Microsoft Copilot Studio prominently.",
    )
    assert summary is None
