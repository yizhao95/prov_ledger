"""Circuit breakers (Phase 1 design).

3 breakers, 2 severity levels:
- SoftStop: surfaceable to LLM as a tool error, recoverable in-session
- HardStop: pause agent, alert user, requires human intervention
"""
from __future__ import annotations


class CircuitBreakerError(Exception):
    """Base class for all circuit breaker errors."""


class SoftStop(CircuitBreakerError):
    """Recoverable. LLM should adjust strategy and retry."""


class HardStop(CircuitBreakerError):
    """Non-recoverable. Pause agent, escalate to user."""


# ── Breaker #1: Immutability of completed steps ───────────────────────────────
def check_immutability(current_status: str) -> None:
    """Raise SoftStop if attempting to modify a COMPLETED step."""
    if current_status == "COMPLETED":
        raise SoftStop(
            "Cannot modify a COMPLETED step. "
            "Insert a retry sub-task with a new step_id instead."
        )


# ── Breaker #2: Loop prevention (max_revisions) ───────────────────────────────
def check_loop_prevention(revision_count: int, max_revisions: int = 5) -> str | None:
    """Check revision_count against max. Returns warning string if approaching, raises HardStop at limit."""
    if revision_count >= max_revisions:
        raise HardStop(
            f"Plan exceeded max_revisions ({revision_count}/{max_revisions}). "
            "Pause and escalate to user. Options: raise max_revisions, restructure plan, or abandon."
        )
    if revision_count == max_revisions - 1:
        return (
            f"⚠️  Plan approaching max revisions ({revision_count}/{max_revisions}). "
            "Sanity-check the approach before next revision."
        )
    return None


# ── Breaker #3: Depth limit ───────────────────────────────────────────────────
def check_depth_limit(parent_depth: int, max_depth: int = 3) -> None:
    """Raise SoftStop if attempting to nest beyond max_depth."""
    if parent_depth >= max_depth:
        raise SoftStop(
            f"Cannot nest sub-tasks deeper than depth {max_depth} "
            f"(parent already at depth {parent_depth}). "
            "Propose a sibling task at the parent's level instead."
        )
