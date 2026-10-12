"""Domain model validation: patterns, normalization, deterministic ids."""

import pytest
from pydantic import ValidationError

from agent_platform.domain.models import (
    ArtifactRef,
    PlanJobSpec,
    PlanResult,
    Task,
    TaskStatus,
    approval_is_expired,
    derive_approval_id,
    derive_task_id,
)


def test_repository_normalized_to_lowercase():
    task = Task(
        task_id=derive_task_id("owner/repo", 1),
        repository="Owner/Repo",
        issue_number=1,
        created_at="2026-09-15T00:00:00Z",
        updated_at="2026-09-15T00:00:00Z",
    )
    assert task.repository == "owner/repo"


def test_repository_rejects_slashless_value():
    with pytest.raises(ValidationError):
        Task(
            task_id="t",
            repository="invalid",
            issue_number=1,
            created_at="2026-09-15T00:00:00Z",
            updated_at="2026-09-15T00:00:00Z",
        )


def test_task_id_deterministic():
    a = derive_task_id("owner/repo", 7)
    b = derive_task_id("OWNER/repo", 7)
    assert a == b
    assert a.startswith("task-")
    assert len(a) == len("task-") + 12


def test_task_rejects_bad_base_sha():
    with pytest.raises(ValidationError):
        Task(
            task_id="t",
            repository="o/r",
            issue_number=1,
            base_sha="xyz",
            created_at="2026-09-15T00:00:00Z",
            updated_at="2026-09-15T00:00:00Z",
        )


def test_artifact_ref_pattern_and_digest():
    ref = ArtifactRef(uri="artifact://" + "0" * 64)
    assert ref.digest == "0" * 64
    with pytest.raises(ValidationError):
        ArtifactRef(uri="artifact://nothex")


def test_plan_result_rejects_extra_fields():
    with pytest.raises(ValidationError):
        PlanResult(
            task_id="t",
            repository="o/r",
            base_sha="a" * 40,
            summary="s",
            unknown_field="nope",
        )


def test_plan_result_rejects_bad_plan_hash_format():
    with pytest.raises(ValidationError):
        PlanResult(
            task_id="t",
            repository="o/r",
            base_sha="a" * 40,
            summary="s",
            plan_hash="not-a-hash",
        )


def test_plan_job_spec_pattern():
    spec = PlanJobSpec(
        task_id="t",
        repository="o/r",
        issue_number=3,
        base_branch="main",
        base_sha="a" * 40,
    )
    assert spec.schema_version == "1"
    with pytest.raises(ValidationError):
        PlanJobSpec(
            task_id="t",
            repository="o/r",
            issue_number=3,
            base_branch="main",
            base_sha="short",
        )


def test_approval_expiry_boundary():
    approval = _approval(expires_at="2026-09-18T09:00:00Z")
    assert not approval_is_expired(approval, "2026-09-18T08:59:59Z")
    assert approval_is_expired(approval, "2026-09-18T09:00:00Z")  # at == expires
    assert approval_is_expired(approval, "2026-09-19T00:00:00Z")


def test_approval_expiry_naive_timestamp_rejected():
    approval = _approval(expires_at="2026-09-18T09:00:00Z")
    with pytest.raises(ValueError):
        approval_is_expired(approval, "2026-09-18T09:00:00")  # no tz


def test_derive_approval_id_stable():
    assert derive_approval_id("t", "sha256:" + "0" * 64) == derive_approval_id(
        "t", "sha256:" + "0" * 64
    )
    assert derive_approval_id("t", "sha256:" + "0" * 64) != derive_approval_id(
        "t", "sha256:" + "1" * 64
    )


def test_task_status_values():
    assert {s.value for s in TaskStatus} == {
        "RECEIVED",
        "PLANNING",
        "AWAITING_APPROVAL",
        "APPROVED",
        "EXECUTING",
        "READY_TO_PUBLISH",
        "PR_CREATED",
        "REVIEWING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "NEEDS_REAPPROVAL",
    }


def _approval(**overrides):
    from agent_platform.domain.models import Approval

    data = dict(
        approval_id="appr-x",
        task_id="t",
        plan_hash="sha256:" + "0" * 64,
        repository="o/r",
        base_branch="main",
        base_sha="a" * 40,
        allowed_paths=["src/x.py"],
        test_commands=["pytest -q"],
        approver="alice",
        approved_at="2026-09-15T09:00:00Z",
        expires_at="2026-09-18T09:00:00Z",
    )
    data.update(overrides)
    return Approval(**data)
