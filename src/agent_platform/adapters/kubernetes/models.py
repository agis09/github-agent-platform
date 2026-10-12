"""Kubernetes Job launcher data models."""

import enum

from pydantic import BaseModel, ConfigDict, Field


class JobPhase(enum.StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class JobKind(enum.StrEnum):
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    REVIEW = "REVIEW"


class JobLaunchSpec(BaseModel):
    """Everything the launcher needs to render and start one sandbox Job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    repository: str
    issue_number: int = Field(ge=1)
    base_branch: str
    base_sha: str
    issue_title: str
    issue_body: str
    plan_image: str
    kind: JobKind = JobKind.PLAN
    plan_hash: str = ""
    allowed_paths: list[str] = []
    test_commands: list[str] = []
    # Forbidden search vocabulary (execute only) — rendered into the
    # job env for the worker's search CLI sanitizer.
    search_forbidden_tokens: list[str] = []
    snapshot_configmap: str = ""  # execute only: git bundle ConfigMap
    prompt: str = ""  # execute only: agent prompt built from the plan
    deadline_seconds: int = Field(default=900, ge=1)
    ttl_seconds: int = Field(default=600, ge=0)


class JobHandle(BaseModel):
    """Identity of a launched job (stable across polling calls)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_name: str
    namespace: str


class JobStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: JobPhase
    reason: str = ""
    log_tail: str = ""
