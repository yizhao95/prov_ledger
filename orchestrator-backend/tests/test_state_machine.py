"""Tests for state_machine.py — transition rules."""
import pytest

from orchestrator.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    StepStatus,
    validate_transition,
)


def test_pending_to_in_progress_allowed():
    validate_transition(StepStatus.PENDING, StepStatus.IN_PROGRESS)
    validate_transition("PENDING", "IN_PROGRESS")  # str form too


def test_pending_to_starting_allowed():
    validate_transition(StepStatus.PENDING, StepStatus.STARTING)


def test_in_progress_to_completed_allowed():
    validate_transition(StepStatus.IN_PROGRESS, StepStatus.COMPLETED)


def test_in_progress_to_failed_allowed():
    validate_transition(StepStatus.IN_PROGRESS, StepStatus.FAILED)


def test_failed_to_in_progress_allowed():
    """User-unblock path."""
    validate_transition(StepStatus.FAILED, StepStatus.IN_PROGRESS)


def test_completed_to_anything_rejected():
    """COMPLETED is terminal — circuit breaker #1 (immutability)."""
    for nxt in StepStatus:
        with pytest.raises(InvalidTransitionError):
            validate_transition(StepStatus.COMPLETED, nxt)


def test_pending_to_completed_rejected():
    """Cannot skip IN_PROGRESS."""
    with pytest.raises(InvalidTransitionError) as exc:
        validate_transition("PENDING", "COMPLETED")
    assert "PENDING → COMPLETED" in str(exc.value)


def test_pending_to_failed_rejected():
    """Must enter IN_PROGRESS or STARTING first to fail meaningfully."""
    with pytest.raises(InvalidTransitionError):
        validate_transition("PENDING", "FAILED")


def test_starting_to_in_progress_allowed():
    validate_transition(StepStatus.STARTING, StepStatus.IN_PROGRESS)


def test_completed_terminal_in_table():
    assert ALLOWED_TRANSITIONS[StepStatus.COMPLETED] == set()


# ── NEEDS_REVIEW state (LLM-reviewed plan completion) ───────────────────────


def test_needs_review_is_known_state():
    """NEEDS_REVIEW must be a recognized StepStatus."""
    assert StepStatus("NEEDS_REVIEW") == StepStatus.NEEDS_REVIEW


def test_pending_to_needs_review_allowed():
    """Deterministic detect flips the review step PENDING -> NEEDS_REVIEW."""
    validate_transition(StepStatus.PENDING, StepStatus.NEEDS_REVIEW)
    validate_transition("PENDING", "NEEDS_REVIEW")


def test_needs_review_to_completed_allowed():
    """After a clean agent review, the review step closes COMPLETED."""
    validate_transition(StepStatus.NEEDS_REVIEW, StepStatus.COMPLETED)


def test_needs_review_to_failed_allowed():
    """When the agent review finds code gaps, the review step FAILS."""
    validate_transition(StepStatus.NEEDS_REVIEW, StepStatus.FAILED)


def test_needs_review_not_terminal():
    """NEEDS_REVIEW is a waiting state, not terminal — it has outgoing edges."""
    assert ALLOWED_TRANSITIONS[StepStatus.NEEDS_REVIEW] != set()
    assert StepStatus.COMPLETED in ALLOWED_TRANSITIONS[StepStatus.NEEDS_REVIEW]
    assert StepStatus.FAILED in ALLOWED_TRANSITIONS[StepStatus.NEEDS_REVIEW]


def test_completed_to_needs_review_rejected():
    """Cannot reopen a COMPLETED step into review."""
    with pytest.raises(InvalidTransitionError):
        validate_transition("COMPLETED", "NEEDS_REVIEW")
