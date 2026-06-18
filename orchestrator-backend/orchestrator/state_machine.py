"""State machine for step transitions.

Allowed transitions (everything else raises InvalidTransitionError):

    PENDING       → STARTING, IN_PROGRESS
    STARTING      → IN_PROGRESS, FAILED
    IN_PROGRESS   → COMPLETED, FAILED
    FAILED        → IN_PROGRESS              (after user unblocks)
    PENDING       → NEEDS_REVIEW             (review step awaits LLM sub-agent)
    NEEDS_REVIEW  → COMPLETED, FAILED        (after agent review verdict)
    COMPLETED     → ∅                        (immutable — circuit breaker #1)
"""
from __future__ import annotations

from enum import Enum


class StepStatus(str, Enum):
    PENDING = "PENDING"
    STARTING = "STARTING"
    IN_PROGRESS = "IN_PROGRESS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS: dict[StepStatus, set[StepStatus]] = {
    StepStatus.PENDING: {
        StepStatus.STARTING,
        StepStatus.IN_PROGRESS,
        StepStatus.NEEDS_REVIEW,
    },
    StepStatus.STARTING: {StepStatus.IN_PROGRESS, StepStatus.FAILED},
    StepStatus.IN_PROGRESS: {StepStatus.COMPLETED, StepStatus.FAILED},
    StepStatus.NEEDS_REVIEW: {StepStatus.COMPLETED, StepStatus.FAILED},
    StepStatus.FAILED: {StepStatus.IN_PROGRESS},
    StepStatus.COMPLETED: set(),  # immutable
}


class InvalidTransitionError(Exception):
    """Raised when a step is asked to transition to an invalid next state."""


def validate_transition(current: str | StepStatus, new: str | StepStatus) -> None:
    """Raise InvalidTransitionError if transition is not allowed."""
    cur = StepStatus(current) if isinstance(current, str) else current
    nxt = StepStatus(new) if isinstance(new, str) else new
    if nxt not in ALLOWED_TRANSITIONS[cur]:
        raise InvalidTransitionError(
            f"Forbidden state transition: {cur.value} → {nxt.value}. "
            f"Allowed from {cur.value}: "
            f"{sorted(s.value for s in ALLOWED_TRANSITIONS[cur]) or '∅ (terminal)'}"
        )
