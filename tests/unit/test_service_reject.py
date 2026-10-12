"""TaskService.reject: state gating, hash match, duplicate replays,
audit trail identity."""

import pytest

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


def test_reject_moves_to_cancelled(service):
    outcome = _plan(service)
    result = service.reject(
        task_id=outcome.task.task_id,
        plan_hash=outcome.task.plan_hash,
        approver="bob",
        reason="scope too wide",
    )
    assert result.task.status == TaskStatus.CANCELLED
    assert result.duplicate is False


def test_reject_duplicate_replay(service):
    outcome = _plan(service)
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    first = service.reject(task_id=task_id, plan_hash=plan_hash, approver="bob")
    second = service.reject(task_id=task_id, plan_hash=plan_hash, approver="bob")
    assert first.duplicate is False
    assert second.duplicate is True


def test_reject_wrong_hash_rejected(service):
    outcome = _plan(service)
    wrong = "sha256:" + "0" * 64
    with pytest.raises(PlanHashMismatchError):
        service.reject(
            task_id=outcome.task.task_id, plan_hash=wrong, approver="bob"
        )


def test_reject_unknown_task(service):
    with pytest.raises(NotFoundError):
        service.reject(
            task_id="task-000000000000",
            plan_hash="sha256:" + "0" * 64,
            approver="bob",
        )


def test_reject_rejected_task_not_repeatable_with_other_approver(service):
    # After CANCELLED (terminal), any further reject is a state conflict.
    outcome = _plan(service)
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    service.reject(task_id=task_id, plan_hash=plan_hash, approver="bob")
    with pytest.raises(StateConflictError):
        service.reject(task_id=task_id, plan_hash=plan_hash, approver="carol")


def test_reject_records_identity_in_audit_trail(service):
    outcome = _plan(service)
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    service.reject(
        task_id=task_id, plan_hash=plan_hash, approver="bob", reason="too risky"
    )
    events = service.status(task_id=task_id).events
    last = events[-1]
    assert last["to"] == "CANCELLED"
    assert "rejected by bob" in last["reason"]
    assert "too risky" in last["reason"]


def test_reject_approved_task_is_state_conflict(service):
    outcome = _plan(service)
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="alice")
    with pytest.raises(StateConflictError):
        service.reject(task_id=task_id, plan_hash=plan_hash, approver="bob")
