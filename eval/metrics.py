"""The two deepeval judges this project uses to grade generated resumes.

Two different tools for two different questions, matching deepeval's own split
between its general-purpose metrics:

* ``resume_groundedness_geval`` — a ``GEval``: one holistic, chain-of-thought
  read of whether the moulded resume is honest about the candidate's real
  history. Best suited to a soft, subjective judgment call — "does this still
  sound like the same person's work" is not arithmetic.

* ``resume_quality_dag`` — a ``DAGMetric``: a deterministic decision tree.
  A fabricated employer or an invented metric is a hard 0 no matter how well
  the rest of the resume reads, and only a resume that clears that gate gets
  scored on how well it addresses the JD. A single holistic score would let a
  beautifully-written hallucination average out against three honest lines —
  the DAG's whole point is refusing to let that happen.

* ``chat_edit_conversational_dag`` — the same deterministic shape, applied to
  the Studio's multi-turn chat-edit agent (section 22) instead of a one-shot
  generation: a ``ConversationalDAGMetric`` over the turn history.

All three take a ``ProjectLLM`` so the judge runs on whatever model this
deployment already has configured, rather than requiring a separate key.
"""

from __future__ import annotations

from deepeval.metrics import ConversationalDAGMetric, DAGMetric, GEval
from deepeval.metrics.conversational_dag import (
    ConversationalBinaryJudgementNode,
    ConversationalNonBinaryJudgementNode,
)
from deepeval.metrics.dag import BinaryJudgementNode, DeepAcyclicGraph, NonBinaryJudgementNode
from deepeval.test_case import MultiTurnParams, SingleTurnParams

from eval.llm_model import ProjectLLM

#: Below this, deepeval marks the test case as failed rather than merely
#: scored — a resume with any hard fabrication should never pass.
DEFAULT_THRESHOLD = 0.6


def resume_groundedness_geval(model: ProjectLLM | None = None) -> GEval:
    """Faithfulness of the moulded text to the candidate's recorded history.

    ``context`` carries the ORIGINAL project/experience text, before moulding
    — the base truth the moulded resume (``actual_output``) is judged against.
    ``input`` carries the job description, so the judge can also tell a
    legitimate JD-driven reframing from a drift into unrelated work.
    """
    return GEval(
        name="Resume Groundedness",
        evaluation_steps=[
            "Compare each claim in 'actual_output' against the candidate's ORIGINAL "
            "recorded project and employment text supplied in 'context'.",
            "A renamed project title, a reworded bullet, reordered emphasis, or "
            "vocabulary drawn from the job description in 'input' is NOT a "
            "violation, as long as it still describes the same underlying system "
            "and the same work.",
            "A fabricated employer, an invented metric or number, a technology "
            "absent from 'context', or a project description that points at a "
            "materially different system than the original one IS a violation — "
            "flag it specifically as a hallucination, not as 'weak phrasing'.",
            "Penalize overclaimed seniority, ownership or scope — 'architected' for "
            "what 'context' describes as 'contributed to', an invented team size, "
            "an invented reporting line — even where no fact is outright invented.",
            "Do not penalize 'actual_output' for omitting parts of 'context' that "
            "were reasonably dropped to fit a one-page resume.",
            "Score low (near 0) when any hallucination or overclaim is present, "
            "regardless of how well-written the rest of the resume is. Score high "
            "(near 10) only when every claim traces back to 'context' or to "
            "honest reframing toward 'input'.",
        ],
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT, SingleTurnParams.CONTEXT],
        model=model or ProjectLLM(),
        threshold=DEFAULT_THRESHOLD,
    )


def resume_quality_dag(model: ProjectLLM | None = None) -> DAGMetric:
    """Fabrication gate, then JD fit — two questions a single score would blur."""
    fabrication_gate = BinaryJudgementNode(
        criteria=(
            "Does 'actual_output' state an employer, project, technology, metric, "
            "team size or scope of responsibility that is absent from 'context', "
            "the candidate's original recorded history? Honest reframing — a "
            "renamed title, a reworded description of the SAME work — does not "
            "count; only content with no basis in 'context' counts."
        ),
        evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT, SingleTurnParams.CONTEXT],
    )
    jd_fit = NonBinaryJudgementNode(
        criteria=(
            "How well does 'actual_output' address the mandatory requirements and "
            "responsibilities stated in 'input', the job description?"
        ),
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
    )
    jd_fit.add_verdict(verdict="Weak", score=2)
    jd_fit.add_verdict(verdict="Adequate", score=6)
    jd_fit.add_verdict(verdict="Strong", score=10)

    fabrication_gate.add_verdict(verdict=True, score=0)
    fabrication_gate.add_verdict(verdict=False, then=jd_fit)

    return DAGMetric(
        name="Resume Quality Gate",
        dag=DeepAcyclicGraph(root_nodes=[fabrication_gate]),
        model=model or ProjectLLM(),
        threshold=DEFAULT_THRESHOLD,
    )


def chat_edit_conversational_dag(model: ProjectLLM | None = None) -> ConversationalDAGMetric:
    """The same fabrication-then-quality shape, for the multi-turn edit agent.

    ``chat_edit`` (section 22) is the one genuinely multi-turn "agentic" path
    in this codebase — a conversation, not a single generation — so it is
    judged with the conversational nodes rather than the single-turn ones.
    """
    grounding_gate = ConversationalBinaryJudgementNode(
        criteria=(
            "Across every assistant turn in this conversation, did the assistant "
            "ever state or apply an edit introducing a fact — an employer, a "
            "metric, a technology, a scope of responsibility — that has no basis "
            "anywhere earlier in the conversation or in the resume state it was "
            "given? Rewriting, condensing or reordering existing content is not a "
            "violation; inventing content is."
        ),
        evaluation_params=[MultiTurnParams.ROLE, MultiTurnParams.CONTENT],
    )
    satisfaction = ConversationalNonBinaryJudgementNode(
        criteria=(
            "Across the conversation, how well did the assistant satisfy the "
            "user's editing instructions?"
        ),
        evaluation_params=[MultiTurnParams.ROLE, MultiTurnParams.CONTENT],
    )
    satisfaction.add_verdict(verdict="Ignored", score=0)
    satisfaction.add_verdict(verdict="Partially satisfied", score=5)
    satisfaction.add_verdict(verdict="Fully satisfied", score=10)

    grounding_gate.add_verdict(verdict=True, score=0)
    grounding_gate.add_verdict(verdict=False, then=satisfaction)

    return ConversationalDAGMetric(
        name="Chat Edit Quality Gate",
        dag=DeepAcyclicGraph(root_nodes=[grounding_gate]),
        model=model or ProjectLLM(),
        threshold=DEFAULT_THRESHOLD,
    )
