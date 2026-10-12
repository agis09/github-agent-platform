"""TaskService.approve: hash matching, state gating, duplicate/conflict,
expiry computation, immutability of allowed_paths."""

import pytest
from pydantic import ValidationError

from agent_platform.domain.models import TaskStatus
from agent_platform.errors import (
    NotFoundError,
    PlanHashMismatchError,
    StateConflictError,
)
from tests.conftest import BASE_SHA


def _plan(service, repository="owner/repo", issue=1):
    return service.plan(
        repository=repository, issue_number=issue, base_branch="main", base_sha=BASE_SHA
    )


def test_approve_success_transitions_to_approved(service):
    outcome = _plan(service)
    result = service.approve(
        task_id=outcome.task.task_id,
        plan_hash=outcome.task.plan_hash,
        approver="alice",
    )
    assert result.task.status == TaskStatus.APPROVED
    assert not result.duplicate
    assert result.approval.approver == "alice"


def test_approve_wrong_hash_rejected_state_unchanged(service):
    outcome = _plan(service)
    with pytest.raises(PlanHashMismatchError):
        service.approve(
            task_id=outcome.task.task_id,
            plan_hash="sha256:" + "0" * 64,
            approver="alice",
        )
    assert service._repo.get_task(outcome.task.task_id).status == TaskStatus.AWAITING_APPROVAL


def test_approve_unknown_task(service):
    _plan(service)
    with pytest.raises(NotFoundError):
        service.approve(
            task_id="task-does-not-exist",
            plan_hash="sha256:" + "0" * 64,
            approver="alice",
        )


def test_approve_not_awaiting_state_rejected(service):
    outcome = _plan(service)
    service.approve(
        task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash, approver="alice"
    )
    with pytest.raises(StateConflictError):
        service.approve(
            task_id=outcome.task.task_id,
            plan_hash=outcome.task.plan_hash,
            approver="bob",
        )


def test_approve_same_content_replays_as_duplicate(service):
    outcome = _plan(service)
    first = service.approve(
        task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash, approver="alice"
    )
    second = service.approve(
        task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash, approver="alice"
    )
    assert first.duplicate is False
    assert second.duplicate is True
    assert first.approval.approval_id == second.approval.approval_id


def test_approve_copies_allowed_paths_and_commands(service):
    outcome = _plan(service)
    result = service.approve(
        task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash, approver="alice"
    )
    assert result.approval.allowed_paths == [c.path for c in outcome.plan.proposed_changes]
    assert result.approval.test_commands == list(outcome.plan.test_commands)


def test_approve_freezes_base_sha(service):
    outcome = _plan(service)
    result = service.approve(
        task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash, approver="alice"
    )
    assert result.approval.base_sha == BASE_SHA


def test_approve_expires_at_computed_from_ttl(service, clock):
    outcome = _plan(service)
    result = service.approve(
        task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash, approver="alice",
        ttl_hours=24,
    )
    assert result.approval.approved_at == "2026-09-15T09:00:00Z"
    assert result.approval.expires_at == "2026-09-16T09:00:00Z"


def test_approve_uses_default_ttl(service, settings, clock):
    outcome = _plan(service)
    result = service.approve(
        task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash, approver="alice"
    )
    assert result.approval.expires_at == "2026-09-18T09:00:00Z"  # default 72h


def test_approval_record_is_immutable(service):
    outcome = _plan(service)
    result = service.approve(
        task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash, approver="alice"
    )
    with pytest.raises(ValidationError):
        result.approval.model_copy(update={"approver": "bob"})  # frozen model
        result.approval.approver = "bob"  # type: ignore[misc]
