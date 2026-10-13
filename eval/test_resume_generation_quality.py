"""LLM-judged evaluation of base resume generation (section 19).

Two custom deepeval metrics grade the same generated resume from different
angles — see ``eval/metrics.py`` for why one score is not enough: GEval gives
the subjective faithfulness read, DAGMetric gives the deterministic
fabrication-then-fit gate, where any hallucination is a hard 0 regardless of
how well the rest reads.

Run explicitly — this is not part of the default ``pytest`` run:
    pytest eval/test_resume_generation_quality.py -v -s
"""

from __future__ import annotations

from deepeval.test_case import LLMTestCase

from app.config.database import SessionLocal
from app.models.person import PersonProfile
from app.models.requirement import Requirement
from app.models.vendor import Vendor
from app.services import resume_composer
from app.services.resume_session_service import create_session
from eval.metrics import resume_groundedness_geval, resume_quality_dag

#: Deliberately a role distant from the seeded profiles (data/ML backgrounds):
#: forces real moulding rather than a near-identical pass-through, which is
#: the scenario where a fabrication is most likely to slip in.
JD_TEXT = """Senior Site Reliability Engineer

We are hiring a Site Reliability Engineer to own the reliability of our
production platform.

Responsibilities:
- Operate and improve CI/CD pipelines and deployment automation
- Build and maintain Kubernetes infrastructure across multiple environments
- Define and track SLOs/SLIs, and lead incident response
- Automate infrastructure provisioning with Terraform

Required skills: Kubernetes, Terraform, Docker, AWS, CI/CD
Preferred skills: Python, Monitoring and Observability
"""


def _resume_text(content) -> str:
    lines = [f"Headline: {content.contact.headline}", f"Summary: {content.summary}"]
    for p in content.projects:
        bullets = ", ".join(p.responsibilities + p.achievements)
        lines.append(
            f"Project: {p.name} — {p.description} [{bullets}] "
            f"(technologies: {', '.join(p.technologies) or 'none listed'})"
        )
    for e in content.experience:
        lines.append(f"Experience: {e.company} as {e.designation}")
    return "\n".join(lines)


def _original_context(source) -> list[str]:
    """The candidate's recorded history, as the judge must see it.

    ``technologies`` belongs here as much as the prose does. It is the stack
    the bank actually records against each project (``ProjectTechnology``), and
    leaving it out made the judge score honest work as fabrication: the seed's
    Retail Demand Forecasting Platform genuinely carries PostgreSQL and Spark,
    but with only the description in context the judge could not know that and
    reported both as invented. Every technology the moulded resume names is
    compared against this list, so a context missing it measures the eval's own
    blind spot rather than the model's grounding.
    """
    if source is None:
        return []
    context = [
        f"Original project: {p.name} — {p.description} "
        f"[{', '.join(p.responsibilities + p.achievements)}] "
        f"(recorded technologies: {', '.join(p.technologies) or 'none recorded'})"
        for p in source.projects
    ]
    context += [f"Original employment: {e.company} as {e.designation}" for e in source.experience]
    return context


def _generate_test_case() -> LLMTestCase:
    """Run the real generation pipeline once and package the result for deepeval."""
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

        session = create_session(
            db,
            requirement_id=requirement.id,
            person_id=profile.person_id,
            person_profile_id=profile.id,
        )
        db.flush()

        composition = resume_composer.generate_base_content(db, session)

        return LLMTestCase(
            input=JD_TEXT,
            actual_output=_resume_text(composition.content),
            context=_original_context(composition.source_content) or None,
        )
    finally:
        # Nothing here should be kept — this is a throwaway probe of the
        # generation pipeline, not a real reservation or a real requirement.
        db.rollback()
        db.close()


def test_generated_resume_is_grounded_in_the_candidates_history() -> None:
    test_case = _generate_test_case()
    metric = resume_groundedness_geval()
    metric.measure(test_case)
    print(f"\n[{metric.name}] score={metric.score:.2f}\nreason: {metric.reason}")
    assert metric.score is not None and metric.score >= metric.threshold, metric.reason


def test_generated_resume_passes_the_quality_gate() -> None:
    test_case = _generate_test_case()
    metric = resume_quality_dag()
    metric.measure(test_case)
    print(f"\n[{metric.name}] score={metric.score:.2f}\nreason: {metric.reason}")
    assert metric.score is not None and metric.score >= metric.threshold, metric.reason
