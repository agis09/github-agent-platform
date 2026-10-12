"""Exception hierarchy and CLI exit codes.

Domain rejections surface as exceptions — no fallbacks that hide failures.
"""


class AgentPlatformError(Exception):
    """Base class. Exit code 1."""

    exit_code = 1


class ArgumentPatternError(AgentPlatformError):
    """CLI argument with a bad format (e.g. malformed SHA). Exit code 2."""

    exit_code = 2


class DomainRejectedError(AgentPlatformError):
    """A domain rule rejected the operation. Exit code 3."""

    exit_code = 3


class StateConflictError(DomainRejectedError):
    """Operation not allowed in the current task state."""


class TaskStateRejectedError(StateConflictError):
    """An issue command does not apply to the issue's latest task in its
    current state. Carries the task id, status and whether the plan waits
    for an answer, so the rejection can be reported on the issue in fixed
    vocabulary (never the exception text)."""

    def __init__(
        self, msg: str, *, task_id: str, status: str, needs_input: bool = False
    ) -> None:
        super().__init__(msg)
        self.task_id = task_id
        self.status = status
        self.needs_input = needs_input


class InvalidTransitionError(StateConflictError):
    """State machine has no edge for the requested transition."""


class PlanHashMismatchError(DomainRejectedError):
    """Supplied plan_hash does not match the recorded one."""


class PlanMismatchError(DomainRejectedError):
    """Stored plan artifact disagrees with the task record."""


class ApprovalConflictError(DomainRejectedError):
    """Same (task, plan_hash) already approved by a different approver."""


class ExpiredApprovalError(DomainRejectedError):
    """Approval record exists but is past its expiry."""


class ArtifactSizeLimitError(DomainRejectedError):
    """Artifact exceeds the configured size limit."""


class UnsupportedSchemaError(AgentPlatformError):
    """Database schema version is not supported (no auto-migration)."""


class NotFoundError(AgentPlatformError):
    """Requested object does not exist. Exit code 4."""

    exit_code = 4


class IntegrityError(AgentPlatformError):
    """Stored data is corrupted or inconsistent. Exit code 5."""

    exit_code = 5


class ArtifactCorruptionError(IntegrityError):
    """Artifact bytes do not match their content hash."""


class ArtifactNotFoundError(NotFoundError):
    """Artifact file referenced by a valid digest is missing."""


class AdapterError(AgentPlatformError):
    """Agent adapter failed to produce a valid result."""


class AdapterTimeoutError(AdapterError):
    """Adapter exceeded its time budget."""


class AdapterContractError(AdapterError):
    """Adapter returned a result that violates the data contract."""


class DependencyNotSatisfiedError(DomainRejectedError):
    """A recorded parent task has not completed; this task must wait."""
