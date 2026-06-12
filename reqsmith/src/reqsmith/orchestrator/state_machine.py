"""Run lifecycle state machine.

Every state and every legal transition is enumerated here; anything else is rejected
at the boundary (design doc §1 "state machine discipline"). Transitions are the ONLY
way run.state changes — repositories must call `transition()`, never set state directly.
"""

from reqsmith.persistence.models import RunState

# state -> set of states it may legally move to
LEGAL_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.INTAKE: frozenset({RunState.TRIAGE, RunState.FAILED, RunState.QUARANTINED}),
    RunState.TRIAGE: frozenset(
        {RunState.AWAITING_INPUT, RunState.RETRIEVAL, RunState.FAILED, RunState.QUARANTINED}
    ),
    RunState.AWAITING_INPUT: frozenset(
        {RunState.TRIAGE, RunState.ESCALATED, RunState.FAILED, RunState.QUARANTINED}
    ),
    RunState.RETRIEVAL: frozenset(
        {RunState.ELICITATION, RunState.DRAFTING, RunState.FAILED, RunState.QUARANTINED}
    ),
    RunState.ELICITATION: frozenset(
        {RunState.DRAFTING, RunState.AWAITING_INPUT, RunState.ESCALATED, RunState.FAILED,
         RunState.QUARANTINED}
    ),
    RunState.DRAFTING: frozenset({RunState.VERIFICATION, RunState.FAILED, RunState.QUARANTINED}),
    RunState.VERIFICATION: frozenset(
        {
            RunState.REVIEW,           # gates passed (or routed) → human review
            RunState.ELICITATION,      # orphan claims reopened as questions
            RunState.DRAFTING,         # re-draft after fixable gate failures
            RunState.FAILED,
            RunState.QUARANTINED,
        }
    ),
    RunState.REVIEW: frozenset(
        {
            RunState.CHECKER_REVIEW,   # maker approved, high tier → independent checker
            RunState.PUBLISHING,       # approved (low/medium tier)
            RunState.DRAFTING,         # reviewer rejected with edits → re-draft
            RunState.ESCALATED,
            RunState.FAILED,
            RunState.QUARANTINED,
        }
    ),
    RunState.CHECKER_REVIEW: frozenset(
        {RunState.PUBLISHING, RunState.REVIEW, RunState.ESCALATED, RunState.FAILED,
         RunState.QUARANTINED}
    ),
    RunState.PUBLISHING: frozenset({RunState.COMPLETE, RunState.FAILED, RunState.QUARANTINED}),
    RunState.ESCALATED: frozenset(
        {RunState.REVIEW, RunState.ELICITATION, RunState.FAILED, RunState.QUARANTINED}
    ),
    # terminal states
    RunState.COMPLETE: frozenset(),
    RunState.FAILED: frozenset(),
    # quarantine is recoverable by an operator decision only
    RunState.QUARANTINED: frozenset({RunState.TRIAGE, RunState.FAILED}),
}

TERMINAL_STATES = frozenset({RunState.COMPLETE, RunState.FAILED})


class IllegalTransition(Exception):
    def __init__(self, current: RunState, target: RunState):
        self.current = current
        self.target = target
        super().__init__(f"illegal transition: {current.value} -> {target.value}")


def assert_transition(current: RunState, target: RunState) -> None:
    if target not in LEGAL_TRANSITIONS[current]:
        raise IllegalTransition(current, target)


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES
