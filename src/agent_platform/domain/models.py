"""Domain models for the agent platform control plane.

All models are frozen with ``extra="forbid"`` so that unknown fields fail
validation — required later when adapter outputs must match the JSON Schema
contract exactly. Floats are structurally banned to keep canonical JSON
hashing deterministic.
"""

import enum
import hashlib
import re
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

REPOSITORY_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
GIT_SHA_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
PLAN_HASH_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
ARTIFACT_URI_PATTERN: Final = re.compile(r"^artifact://[0-9a-f]{64}$")

_SchemaVersion = Literal["1"]

# Plain string artifact URI, exactly as it appears in the JSON contract
# "artifact://<sha256-hex>".
ArtifactUri = Annotated[str, StringConstraints(pattern=ARTIFACT_URI_PATTERN.pattern)]


class TaskStatus(enum.StrEnum):
    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PR_CREATED = "PR_CREATED"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_REAPPROVAL = "NEEDS_REAPPROVAL"


def derive_task_id(repository: str, issue_number: int, revision: int = 1) -> str:
    """Deterministic task id for a (repository, issue, revision) triple.

    Same triple always yields the same id, so "running plan twice creates no
    duplicate task" is verifiable by explicit id comparison. Repository is
    lowercased first to match Task normalization. revision > 1 identifies a
    re-run of the same issue after it COMPLETED (redo: additional
    feedback spawns a new revision task; the old one stays as history).
    """
    payload = f"task:{repository.lower()}#{issue_number}@r{revision}".encode()
    return "task-" + hashlib.sha256(payload).hexdigest()[:12]


class ArtifactRef(BaseModel):
    """Reference to a content-addressed artifact: artifact://<sha256-hex>."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str = Field(pattern=ARTIFACT_URI_PATTERN.pattern)

    @property
    def digest(self) -> str:
        return self.uri.removeprefix("artifact://")


class Task(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    repository: str
    issue_number: int = Field(ge=1)
    # Redo: 1 = first run; >1 identifies a re-execution of the same
    # issue after its predecessor COMPLETED.
    revision: int = Field(default=1, ge=1)
    # Forbidden search vocabulary derived at plan time from the
    # untrusted task context; enforced by SearchSanitizer at execute time.
    search_forbidden_tokens: list[str] = []
    status: TaskStatus = TaskStatus.RECEIVED
    base_branch: str | None = None
    base_sha: str | None = Field(default=None, pattern=GIT_SHA_PATTERN.pattern)
    plan_hash: str | None = Field(default=None, pattern=PLAN_HASH_PATTERN.pattern)
    execution_ref: str | None = None
    created_at: str
    updated_at: str

    @field_validator("repository")
    @classmethod
    def _normalize_repository(cls, value: str) -> str:
        normalized = value.lower()
        if not REPOSITORY_PATTERN.fullmatch(normalized):
            msg = f"repository must match owner/name, got {value!r}"
            raise ValueError(msg)
        return normalized


class PlanJobSpec(BaseModel):
    """Input for AgentAdapter.plan(). Non-trusted context fields are part of
    the schema as the extension point the prompt builder fills in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: _SchemaVersion = "1"
    task_id: str
    repository: str
    issue_number: int = Field(ge=1)
    base_branch: str
    base_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    issue_title: str | None = None
    issue_body: str | None = None


class ExecuteJobSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: _SchemaVersion = "1"
    task_id: str
    repository: str
    plan_hash: str = Field(pattern=PLAN_HASH_PATTERN.pattern)
    base_branch: str
    base_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    allowed_paths: list[str]
    test_commands: list[str]
    # Forbidden search vocabulary from the approval (derived at plan
    # time from the untrusted task context); enforced by the worker's
    # SearchSanitizer.
    search_forbidden_tokens: list[str] = []
    plan_ref: ArtifactUri


class ReviewJobSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: _SchemaVersion = "1"
    task_id: str
    repository: str
    plan_hash: str = Field(pattern=PLAN_HASH_PATTERN.pattern)
    base_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    head_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    diff_ref: ArtifactUri | None = None
    test_results_ref: ArtifactUri | None = None


class ProposedChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    reason: str


class PlanResult(BaseModel):
    """Structured plan the adapter returns; matches schemas/plan_result.schema.json.

    The adapter returns ``plan_hash`` empty (""), the orchestrator recomputes
    it — adapter-supplied hashes are never trusted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: _SchemaVersion = "1"
    task_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    base_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    summary: str = Field(max_length=10_000)
    assumptions: list[str] = []
    questions: list[str] = []
    proposed_changes: list[ProposedChange] = []
    test_commands: list[str] = []
    risks: list[str] = []
    requires_human_input: bool = False
    plan_hash: str = Field(default="", pattern=r"^(sha256:[0-9a-f]{64})?$")


class CommandRecord(BaseModel):
    """One audited command. Inline stdout/stderr are size-bounded excerpts
    (the sandbox has no remote artifact store); the *_ref fields point at
    full captures once a remote store exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    exit_code: int
    duration_ms: int = Field(ge=0)
    stdout_ref: ArtifactUri | None = None
    stderr_ref: ArtifactUri | None = None
    stdout_text: str | None = Field(default=None, max_length=4096)
    stderr_text: str | None = Field(default=None, max_length=4096)


class TestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    status: Literal["passed", "failed", "skipped", "error"]


class Violation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["out_of_scope_path", "forbidden_command", "output_size", "other"]
    detail: str


class ExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: _SchemaVersion = "1"
    task_id: str
    plan_hash: str = Field(pattern=PLAN_HASH_PATTERN.pattern)
    base_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    status: Literal["succeeded", "failed", "timeout", "cancelled"]
    patch_ref: ArtifactUri | None = None
    changed_files: list[str] = []
    commands: list[CommandRecord] = []
    tests: list[TestResult] = []
    agent_events_ref: ArtifactUri | None = None
    violations: list[Violation] = []
    pr_body_draft: str | None = None


class ReviewFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: Literal["critical", "high", "medium", "low", "info"]
    path: str
    line: int | None = None
    message: str
    evidence: str
    reproduction: str | None = None


class ReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: _SchemaVersion = "1"
    task_id: str
    repository: str
    plan_hash: str = Field(pattern=PLAN_HASH_PATTERN.pattern)
    base_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    head_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    summary: str = ""
    findings: list[ReviewFinding] = []


def derive_approval_id(task_id: str, plan_hash: str) -> str:
    payload = f"approval:{task_id}:{plan_hash}".encode()
    return "appr-" + hashlib.sha256(payload).hexdigest()[:12]


class Approval(BaseModel):
    """Immutable approval record. ``allowed_paths`` / ``test_commands`` are
    copied from the plan at approval time and never change afterwards.
    ``search_forbidden_tokens`` carries the forbidden search vocabulary derived
    from the untrusted task context at plan time; SearchSanitizer refuses
    any search query containing one of these tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str
    task_id: str
    plan_hash: str = Field(pattern=PLAN_HASH_PATTERN.pattern)
    repository: str
    base_branch: str
    base_sha: str
    allowed_paths: list[str]
    test_commands: list[str]
    search_forbidden_tokens: list[str] = []
    approver: str
    approved_at: str
    expires_at: str


def approval_is_expired(approval: Approval, at: str) -> bool:
    """True when ``at`` (RFC3339 UTC) is at or after ``expires_at``."""
    expiry = _parse_utc(approval.expires_at)
    moment = _parse_utc(at)
    return moment >= expiry


def _parse_utc(value: str) -> datetime:
    parsed = datetime_from_rfc3339(value)
    return parsed


def datetime_from_rfc3339(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        msg = f"timestamp must be timezone-aware RFC3339, got {value!r}"
        raise ValueError(msg)
    return parsed

class Artifact(BaseModel):
    """Artifact payload. Metadata (size, retention class) lives in the DB."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    data: bytes


class PublishRequest(BaseModel):
    """Input for the PR Publisher: everything needed to re-verify and publish."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: _SchemaVersion = "1"
    task_id: str
    repository: str
    plan_hash: str = Field(pattern=PLAN_HASH_PATTERN.pattern)
    base_branch: str
    base_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    patch: str
    changed_files: list[str]
    approver: str
    approval_id: str
    approved_at: str
    expires_at: str
    pr_body_draft: str | None = None
    issue_number: int | None = Field(default=None, ge=1)
    plan_summary: str | None = Field(default=None, max_length=200)


class PublishResult(BaseModel):
    """Outcome of a publish. branch_name is deterministic per task; pr_number
    is None when the PR already existed (idempotent replay)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: _SchemaVersion = "1"
    task_id: str
    plan_hash: str = Field(pattern=PLAN_HASH_PATTERN.pattern)
    branch_name: str
    pr_number: int | None = None
    pr_url: str | None = None
    duplicate: bool = False
