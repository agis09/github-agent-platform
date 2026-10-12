"""All state transitions: every legal edge accepted, every other one rejected."""

import pytest

from agent_platform.domain.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    can_transition,
    validate_transition,
)
from agent_platform.errors import InvalidTransitionError

ALL_STATES = sorted(TRANSITIONS.keys() | TERMINAL_STATES)


def test_all_legal_edges_accepted():
    legal = [
        (current, target)
        for current, targets in TRANSITIONS.items()
        for target in targets
    ]
    assert legal, "transition table must not be empty"
    for current, target in legal:
        assert can_transition(current, target), f"{current} -> {target} should be legal"


def test_all_illegal_pairs_rejected():
    illegal = [
        (current, target)
        for current in ALL_STATES
        for target in ALL_STATES
        if target not in TRANSITIONS.get(current, frozenset())
    ]
    for current, target in illegal:
        with pytest.raises(InvalidTransitionError):
            validate_transition(current, target)


def test_no_self_loops():
    for current in ALL_STATES:
        assert not can_transition(current, current)


def test_terminal_states_have_no_outgoing_edges():
    for terminal in TERMINAL_STATES:
        assert TRANSITIONS.get(terminal, frozenset()) == frozenset()


def test_needs_reapproval_edges():
    assert TRANSITIONS["NEEDS_REAPPROVAL"] == frozenset(
        {"PLANNING", "EXECUTING", "CANCELLED"}
    )


def test_no_edges_from_unknown_state():
    assert not can_transition("UNKNOWN", "PLANNING")
    with pytest.raises(InvalidTransitionError):
        validate_transition("UNKNOWN", "PLANNING")


def test_reviewing_can_fail():
    """A crashed review run must not strand the task in REVIEWING."""
    assert can_transition("REVIEWING", "FAILED")
