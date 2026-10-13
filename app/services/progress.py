"""Live checkpoints for a long-running generation, kept in memory.

Generating a resume makes several sequential model calls and can legitimately
run for minutes now that token budgets are sized for quality rather than
speed (see ``LLM_MAX_TOKENS``). Someone watching a spinner with no other
signal cannot tell "still working" from "stuck" — which is exactly the
question that came up while testing. This gives the studio something to
poll: a short, ordered checklist of the stages the pipeline is ACTUALLY
passing through, lit up as each one is reached — not a simulated timer.

In-memory and per-process, which is the right amount of infrastructure for
what this is: one operator watching their own generation, gone the moment it
finishes. It does not need to survive a restart or be visible across
processes, and it costs nothing when nobody is polling it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from uuid import UUID

#: The real stages ``resume_composer.generate_base_content`` and
#: ``resume_session_service.generate_base_resume`` pass through, in order.
#: Kept here rather than inferred from the calls themselves so the list is
#: exactly what a user should be told, independent of implementation detail.
STEPS: tuple[str, ...] = (
    "Selecting projects and companies from the bank",
    "Laying out the timeline",
    "Checking fit against the JD",
    "Moulding the story to the JD",
    "Applying house rules",
    "Writing the summary",
    "Checking facts",
)


@dataclass(slots=True)
class _State:
    index: int = -1
    label: str = ""
    done: bool = False
    error: str | None = None
    started_at: float = field(default_factory=time.time)


_lock = threading.Lock()
#: Bounded implicitly: one entry per session that has ever generated, in this
#: process's lifetime. Never large enough in practice to matter, and a
#: restart clears it — nothing here needs to outlive the process.
_state: dict[str, _State] = {}


def start(session_id: UUID | str) -> None:
    """Mark a fresh generation beginning. Call before the first step."""
    with _lock:
        _state[str(session_id)] = _State()


def step(session_id: UUID | str, label: str) -> None:
    """Move the checklist to ``label``. A no-op if ``start`` was never called.

    Silent on an unknown session rather than raising: this is a progress
    indicator, and a bug in it must never be the reason a generation fails.
    """
    key = str(session_id)
    with _lock:
        state = _state.get(key)
        if state is None:
            return
        try:
            index = STEPS.index(label)
        except ValueError:
            index = state.index + 1
        state.index = index
        state.label = label


def finish(session_id: UUID | str, *, error: str | None = None) -> None:
    """Mark the generation over, successfully or not."""
    key = str(session_id)
    with _lock:
        state = _state.get(key)
        if state is None:
            return
        state.done = True
        state.error = error


def read(session_id: UUID | str) -> dict:
    """The checklist as it stands, for the studio to poll.

    No entry (nothing has ever generated here, or the process restarted) reads
    as an already-finished, inactive state — the honest answer, and the one
    that makes the UI show nothing rather than a checklist stuck at the start.
    """
    key = str(session_id)
    with _lock:
        state = _state.get(key)
        if state is None:
            return {"active": False, "steps": list(STEPS), "index": -1, "done": True, "error": None}
        return {
            "active": not state.done,
            "steps": list(STEPS),
            "index": state.index,
            "label": state.label,
            "done": state.done,
            "error": state.error,
            "elapsed_seconds": round(time.time() - state.started_at, 1),
        }
