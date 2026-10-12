"""Task state machine.

Self-loops are NOT valid edges here — the repository layer treats "transition
to the current state" as an idempotent replay (ALREADY_APPLIED), never as a
transition any caller may invoke as a regular edge.
"""

import enum
from collections.abc import Mapping
from typing import Final

from agent_platform.errors import InvalidTransitionError


class TransitionOutcome(enum.Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"


TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED"}
)

TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "RECEIVED": frozenset({"PLANNING"}),
    "PLANNING": frozenset({"AWAITING_APPROVAL", "FAILED"}),
    "AWAITING_APPROVAL": frozenset({"APPROVED", "CANCELLED"}),
    "APPROVED": frozenset({"EXECUTING", "NEEDS_REAPPROVAL"}),
    "EXECUTING": frozenset({"READY_TO_PUBLISH", "FAILED"}),
    "READY_TO_PUBLISH": frozenset({"PR_CREATED", "NEEDS_REAPPROVAL"}),
    "PR_CREATED": frozenset({"REVIEWING"}),
    # REVIEWING -> FAILED: the review run itself crashed (job launch failure,
    # timeout). Without this edge a crashed review strands the task in
    # REVIEWING with no recovery path — service.review's error handler must
    # be able to record the failure.
    "REVIEWING": frozenset({"COMPLETED", "NEEDS_REAPPROVAL", "READY_TO_PUBLISH", "FAILED"}),
    # NEEDS_REAPPROVAL -> EXECUTING: re-execute an unchanged, still-approved
    # plan (agentctl reexecute). The plan_hash is unchanged, so the approval
    # record stays valid; the active-approval gate re-checks expiry.
    "NEEDS_REAPPROVAL": frozenset({"PLANNING", "EXECUTING", "CANCELLED"}),
    # Terminal states have no outgoing edges.
}


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return False  # self-loops are handled as replay, not transitions
    return target in TRANSITIONS.get(current, frozenset())


def validate_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        msg = f"cannot transition {current} -> {target}"
        raise InvalidTransitionError(msg)
