import pytest

from reqsmith.orchestrator.state_machine import (
    LEGAL_TRANSITIONS,
    IllegalTransition,
    assert_transition,
    is_terminal,
)
from reqsmith.persistence.models import RunState


def test_every_state_has_a_transition_entry():
    assert set(LEGAL_TRANSITIONS) == set(RunState)


def test_terminal_states_have_no_exits():
    assert LEGAL_TRANSITIONS[RunState.COMPLETE] == frozenset()
    assert LEGAL_TRANSITIONS[RunState.FAILED] == frozenset()
    assert is_terminal(RunState.COMPLETE) and is_terminal(RunState.FAILED)


def test_legal_transitions_pass():
    for current, targets in LEGAL_TRANSITIONS.items():
        for target in targets:
            assert_transition(current, target)  # must not raise


def test_illegal_transitions_raise_for_full_matrix():
    for current in RunState:
        for target in RunState:
            if target in LEGAL_TRANSITIONS[current]:
                continue
            with pytest.raises(IllegalTransition):
                assert_transition(current, target)


def test_intake_cannot_jump_to_publishing():
    with pytest.raises(IllegalTransition):
        assert_transition(RunState.INTAKE, RunState.PUBLISHING)


def test_quarantine_is_recoverable_to_triage_only():
    assert LEGAL_TRANSITIONS[RunState.QUARANTINED] == frozenset(
        {RunState.TRIAGE, RunState.FAILED}
    )
