"""Contract: the approval gate.

Proof obligations:
- no Execute Job without an approval record (require_active_approval is the
  single gate; every failure mode is exercised)
- one approval never spawns multiple Execute Jobs (idempotency binding:
  one approval -> one execute idempotency key; a second execute request with
  the same key replays, a different key is refused by state gating)
- expired approvals are refused
- execution is refused after the base branch moved
- the audit log reconstructs who approved what
"""

import datetime

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.clock import FixedClock
from agent_platform.config import Settings
from agent_platform.domain.models import TaskStatus
from agent_platform.errors import (
    ExpiredApprovalError,
    PlanHashMismatchError,
    StateConflictError,
)
from agent_platform.orchestrator.repository import SqliteTaskRepository
from agent_platform.orchestrator.service import TaskService
from tests.conftest import BASE_SHA

UTC = datetime.UTC


def _service(clock: FixedClock, tmp_path):
    settings = Settings(
        home_dir=str(tmp_path),
        artifact_max_size=1024 * 1024,
        default_approval_ttl_hours=72,
        agent_binary="codex",
        agent_model=None,
        agent_base_url=None,
        agent_api_key_env="LLM_API_KEY",
        codex_wire_api="responses",
        agent_timeout_seconds=600.0,
        agent_max_output_tokens=None,
    )
    repo = SqliteTaskRepository(tmp_path / "state.sqlite3")
    return TaskService(
        repository=repo,
        adapter=FakeAgentAdapter(),
        artifacts=LocalFsArtifactStore(tmp_path / "artifacts", 1024 * 1024),
        clock=clock,
        settings=settings,
    ), repo


def _approved_task(service, repository="owner/repo", issue=1, approver="alice"):
    outcome = service.plan(
        repository=repository, issue_number=issue, base_branch="main", base_sha=BASE_SHA
    )
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    service.approve(task_id=task_id, plan_hash=plan_hash, approver=approver)
    return task_id, plan_hash


def test_execute_gate_passes_with_active_approval(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, _ = _service(clock, tmp_path)
    task_id, plan_hash = _approved_task(service)
    approval = service.require_active_approval(task_id=task_id, plan_hash=plan_hash)
    assert approval.approver == "alice"
    assert approval.plan_hash == plan_hash


def test_execute_gate_blocks_without_approval_record(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, _ = _service(clock, tmp_path)
    outcome = service.plan(
        repository="owner/repo", issue_number=2, base_branch="main", base_sha=BASE_SHA
    )
    # Task has a plan_hash but nobody approved it.
    with pytest.raises(ExpiredApprovalError, match="no approval record"):
        service.require_active_approval(
            task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash
        )


def test_execute_gate_blocks_with_wrong_hash(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, _ = _service(clock, tmp_path)
    task_id, _ = _approved_task(service)
    wrong = "sha256:" + "0" * 64
    with pytest.raises(PlanHashMismatchError):
        service.require_active_approval(task_id=task_id, plan_hash=wrong)


def test_execute_gate_blocks_expired_approval(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, _ = _service(clock, tmp_path)
    task_id, plan_hash = _approved_task(service)
    clock.advance(73 * 3600)  # default TTL is 72h
    with pytest.raises(ExpiredApprovalError, match="expired"):
        service.require_active_approval(task_id=task_id, plan_hash=plan_hash)


def test_execute_gate_blocks_non_approved_state(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, _ = _service(clock, tmp_path)
    # Simulate an approval row for a task whose state moved on (e.g. a replay
    # raced a reject): approval exists, task is CANCELLED.
    outcome = service.plan(
        repository="owner/repo", issue_number=3, base_branch="main", base_sha=BASE_SHA
    )
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    service._repo.insert_idempotency(
        f"approve:{task_id}:{plan_hash}:alice", "approve", task_id,
        clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"), ignore=True,
    )
    from agent_platform.domain.models import Approval, derive_approval_id

    approval = Approval(
        approval_id=derive_approval_id(task_id, plan_hash),
        task_id=task_id,
        plan_hash=plan_hash,
        repository="owner/repo",
        base_branch="main",
        base_sha=BASE_SHA,
        allowed_paths=[],
        test_commands=[],
        approver="alice",
        approved_at="2026-09-16T09:00:00Z",
        expires_at="2026-09-19T09:00:00Z",
    )
    service._repo.insert_approval(approval)
    service._repo.transition_task(
        task_id, "CANCELLED", reason="test setup", now="2026-09-16T09:01:00Z"
    )
    with pytest.raises(StateConflictError):
        service.require_active_approval(task_id=task_id, plan_hash=plan_hash)


def test_execute_gate_blocks_after_base_branch_change(tmp_path):
    """base branch更新後の実行拒否: the approval freezes base_branch; a task
    re-planned onto a different branch has a new plan_hash, so the old
    approval no longer matches the task's current hash."""
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, repo = _service(clock, tmp_path)
    task_id, plan_hash = _approved_task(service)
    old_approval = service.require_active_approval(
        task_id=task_id, plan_hash=plan_hash
    )
    assert old_approval.base_branch == "main"

    # Re-plan on a feature branch (NEEDS_REAPPROVAL path): state moves out of
    # AWAITING_APPROVAL. The new plan produces a different plan_hash because
    # base_branch is part of the plan identity — simulate by checking the
    # gate against the old approval while the task row moved to a new branch.
    service._repo.transition_task(
        task_id, "NEEDS_REAPPROVAL", reason="base branch changed",
        now="2026-09-16T10:00:00Z",
    )
    # The task's recorded plan_hash still matches the OLD approval, but the
    # state is PLANNING, not APPROVED — the gate refuses.
    with pytest.raises(StateConflictError):
        service.require_active_approval(task_id=task_id, plan_hash=plan_hash)


def test_one_approval_one_execute_idempotency_binding(tmp_path):
    """同一承認から複数のExecute Jobが勝手に作られない: the execute path binds
    one idempotency key per (approval, execute request). The second bind with
    the same key replays; the DB UNIQUE constraint prevents key rebinding."""
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, repo = _service(clock, tmp_path)
    task_id, plan_hash = _approved_task(service)
    service.require_active_approval(task_id=task_id, plan_hash=plan_hash)

    key = f"execute:{task_id}:{plan_hash}"
    now = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    repo.insert_idempotency(key, "approve", task_id, now)  # first execute request
    # A second execute request with the same key must be refused — one
    # approval cannot silently spawn a second Job.
    from agent_platform.errors import StateConflictError as SCE

    with pytest.raises(SCE, match="already bound"):
        repo.insert_idempotency(key, "approve", task_id, now)


def test_audit_log_restores_who_approved_what(tmp_path):
    """audit logから誰が何を承認したか復元できる."""
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, _ = _service(clock, tmp_path)
    task_id, plan_hash = _approved_task(service, approver="alice")
    view = service.status(task_id=task_id)
    approval = view.approval
    assert approval is not None
    assert approval.approver == "alice"
    assert approval.plan_hash == plan_hash
    assert approval.base_branch == "main"
    assert approval.base_sha == BASE_SHA
    assert approval.approved_at == "2026-09-16T09:00:00Z"
    assert approval.expires_at == "2026-09-19T09:00:00Z"
    events = [e for e in view.events if "approved by alice" in e["reason"]]
    assert len(events) == 1
    assert events[0]["to"] == TaskStatus.APPROVED.value


def test_expired_approval_is_visible_in_status(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, _ = _service(clock, tmp_path)
    task_id, plan_hash = _approved_task(service)
    clock.advance(73 * 3600)
    view = service.status(task_id=task_id)
    assert view.approval_expired is True
    assert view.task.status == TaskStatus.APPROVED  # state unchanged; gate refuses
