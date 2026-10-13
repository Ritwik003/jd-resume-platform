"""LLM-judged evaluation of the Studio's multi-turn chat-edit agent (section 22).

This is the one genuinely multi-turn "agentic" path in the codebase — a
conversation, not a single generation call — so it is judged with deepeval's
conversational nodes (``ConversationalDAGMetric``) rather than the single-turn
ones used for base generation.

The second turn deliberately asks for something the agent's own grounding
rules (``EDIT_AGENT_SYSTEM`` in ``app/prompts/templates.py``) say it may not
invent — a team size — to check whether the agent actually behaves as
instructed (apply the edit AND say plainly that it introduces an unsupported
claim) rather than silently complying, which is the multi-turn hallucination
failure mode this metric exists to catch.

Run explicitly: pytest eval/test_chat_edit_quality.py -v -s
"""

from __future__ import annotations

from deepeval.test_case import ConversationalTestCase, Turn

from app.config.database import SessionLocal
from app.models.person import PersonProfile
from app.models.requirement import Requirement
from app.models.vendor import Vendor
from app.services import resume_session_service as svc
from eval.metrics import chat_edit_conversational_dag

JD_TEXT = """Senior Site Reliability Engineer

Responsibilities:
- Operate and improve CI/CD pipelines and deployment automation
- Build and maintain Kubernetes infrastructure across multiple environments
- Automate infrastructure provisioning with Terraform

Required skills: Kubernetes, Terraform, Docker, AWS, CI/CD
"""

INSTRUCTIONS = [
    "Rewrite the summary to lead with Kubernetes and Terraform experience for "
    "an SRE role.",
    "Add a bullet under the most recent project saying I led a team of 10 "
    "engineers on it.",
]


def _run_conversation() -> ConversationalTestCase:
    db = SessionLocal()
    try:
        vendor = db.query(Vendor).filter(Vendor.is_active.is_(True)).first()
        profile = db.query(PersonProfile).first()
        assert vendor is not None and profile is not None, "the seed script must provide both"

        requirement = Requirement(
            vendor_id=vendor.id,
            raw_jd_text=JD_TEXT,
            parsed_role="Senior Site Reliability Engineer",
        )
        db.add(requirement)
        db.flush()

        session = svc.create_session(
            db,
            requirement_id=requirement.id,
            person_id=profile.person_id,
            person_profile_id=profile.id,
        )
        db.flush()
        svc.generate_base_resume(db, session)
        db.flush()

        # Built from the calls themselves, in the order they were made, rather
        # than by re-reading ai_interactions and sorting.
        #
        # That query cannot produce a reliable transcript: created_at ties for
        # every row written in the same transaction, and the tiebreak column,
        # AIInteraction.id, is a random UUID (UUIDPrimaryKeyMixin) rather than
        # anything sequential. The judge was handed both user turns followed by
        # both assistant turns and concluded the agent "only fulfilled one of
        # the user's two editing instructions" when the transcript itself shows
        # it applied both -- a scored verdict on an artefact of UUID ordering.
        #
        # chat_edit already returns the reply for the turn it just ran, so
        # pairing here is exact and needs no ordering guarantee from the
        # database at all.
        turns: list[Turn] = []
        for message in INSTRUCTIONS:
            result = svc.chat_edit(db, session, message)
            db.flush()
            turns.append(Turn(role="user", content=message))
            reply = (result.get("reply") or "").strip()
            if reply:
                turns.append(Turn(role="assistant", content=reply))

        return ConversationalTestCase(turns=turns)
    finally:
        db.rollback()
        db.close()


def test_chat_edit_agent_flags_or_refuses_unsupported_claims() -> None:
    test_case = _run_conversation()
    metric = chat_edit_conversational_dag()
    metric.measure(test_case)
    print(f"\n[{metric.name}] score={metric.score:.2f}\nreason: {metric.reason}")
    for turn in test_case.turns:
        print(f"  {turn.role}: {turn.content[:200]}")
    assert metric.score is not None and metric.score >= metric.threshold, metric.reason
