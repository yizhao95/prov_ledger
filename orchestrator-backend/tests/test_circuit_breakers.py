"""Tests for circuit_breakers.py — immutability, loop, depth."""
import pytest

from orchestrator.circuit_breakers import (
    HardStop,
    SoftStop,
    check_depth_limit,
    check_immutability,
    check_loop_prevention,
)


def test_immutability_raises_softstop_on_completed():
    with pytest.raises(SoftStop, match="COMPLETED"):
        check_immutability("COMPLETED")


def test_immutability_quiet_on_in_progress():
    check_immutability("IN_PROGRESS")  # no raise


def test_immutability_quiet_on_pending():
    check_immutability("PENDING")


def test_loop_prevention_quiet_below_max():
    assert check_loop_prevention(0, 5) is None
    assert check_loop_prevention(2, 5) is None


def test_loop_prevention_warns_at_max_minus_one():
    warn = check_loop_prevention(4, 5)
    assert warn is not None and "approaching" in warn


def test_loop_prevention_raises_hardstop_at_max():
    with pytest.raises(HardStop, match="exceeded"):
        check_loop_prevention(5, 5)


def test_loop_prevention_raises_hardstop_above_max():
    with pytest.raises(HardStop):
        check_loop_prevention(99, 5)


def test_depth_limit_quiet_below_3():
    check_depth_limit(0)
    check_depth_limit(1)
    check_depth_limit(2)


def test_depth_limit_raises_softstop_at_3():
    with pytest.raises(SoftStop, match="depth"):
        check_depth_limit(3)


def test_depth_limit_custom_max():
    check_depth_limit(4, max_depth=5)
    with pytest.raises(SoftStop):
        check_depth_limit(5, max_depth=5)
