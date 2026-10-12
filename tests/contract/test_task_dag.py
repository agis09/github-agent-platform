"""Contract: parent/child task DAG.

- attach_child records edges idempotently with the approver identity
- cycle detection refuses edges that would close a loop
- dependency gate: a child with an incomplete parent cannot execute/publish
- parent status is a projection over children (never stored)
"""

import datetime

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.clock import FixedClock
from agent_platform.config import Settings
from agent_platform.domain.models import TaskStatus
from agent_platform.errors import (
    DependencyNotSatisfiedError,
    NotFoundError,
    StateConflictError,
)
from agent_platform.orchestrator.repository import SqliteTaskRepository
from agent_platform.orchestrator.service import TaskService
from tests.conftest import BASE_SHA

UTC = datetime.UTC


def _service(tmp_path):
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
    service = TaskService(
        repository=repo,
        adapter=FakeAgentAdapter(),
        artifacts=LocalFsArtifactStore(tmp_path / "artifacts", 1024 * 1024),
        clock=FixedClock(datetime.datetime(2026, 9, 17, 9, 0, 0, tzinfo=UTC)),
        settings=settings,
    )
    return service, repo


def _make_task(service, repository, issue):
    outcome = service.plan(
        repository=repository, issue_number=issue, base_branch="main",
        base_sha=BASE_SHA,
    )
    return outcome.task.task_id, outcome.task.plan_hash


def test_attach_child_records_edge_and_is_idempotent(tmp_path):
    service, repo = _service(tmp_path)
    parent_id, _ = _make_task(service, "owner/parent", 1)
    child_id, _ = _make_task(service, "owner/child-a", 2)

    first = service.attach_child(
        parent_task_id=parent_id, child_task_id=child_id, approver="alice"
    )
    second = service.attach_child(
        parent_task_id=parent_id, child_task_id=child_id, approver="alice"
    )
    assert first.duplicate is False
    assert second.duplicate is True
    assert repo.list_children(parent_id) == [child_id]


def test_attach_child_requires_existing_tasks(tmp_path):
    service, _ = _service(tmp_path)
    with pytest.raises(NotFoundError):
        service.attach_child(
            parent_task_id="task-000000000000",
            child_task_id="task-111111111111",
            approver="alice",
        )


def test_cycle_detection(tmp_path):
    service, _ = _service(tmp_path)
    a, _ = _make_task(service, "owner/repo-a", 1)
    b, _ = _make_task(service, "owner/repo-b", 2)
    service.attach_child(parent_task_id=a, child_task_id=b, approver="alice")
    # b -> a would close the cycle a -> b -> a
    with pytest.raises(StateConflictError, match="cycle"):
        service.attach_child(parent_task_id=b, child_task_id=a, approver="alice")


def test_self_edge_refused(tmp_path):
    service, _ = _service(tmp_path)
    a, _ = _make_task(service, "owner/repo-a", 1)
    with pytest.raises(StateConflictError):
        service.attach_child(
            parent_task_id=a, child_task_id=a, approver="alice"
        )


def test_dependency_gate_blocks_child_execute(tmp_path):
    service, _ = _service(tmp_path)
    parent_id, parent_hash = _make_task(service, "owner/parent", 1)
    child_id, child_hash = _make_task(service, "owner/child", 2)
    service.attach_child(
        parent_task_id=parent_id, child_task_id=child_id, approver="alice"
    )
    # The parent executed once, then its base changed -> NEEDS_REAPPROVAL.
    service.approve(task_id=parent_id, plan_hash=parent_hash, approver="alice")
    service.execute(task_id=parent_id, plan_hash=parent_hash)
    service._repo.transition_task(
        parent_id, TaskStatus.NEEDS_REAPPROVAL, reason="base changed",
        now="2026-09-17T09:05:00Z",
    )
    # Child is approved and ready, but must wait for its parent.
    service.approve(task_id=child_id, plan_hash=child_hash, approver="alice")
    with pytest.raises(DependencyNotSatisfiedError, match="dependency not satisfied"):
        service.execute(task_id=child_id, plan_hash=child_hash)


def test_shell_parent_imposes_no_ordering(tmp_path):
    service, _ = _service(tmp_path)
    parent_id, _ = _make_task(service, "owner/parent", 1)  # planned, never executed
    child_id, child_hash = _make_task(service, "owner/child", 2)
    service.attach_child(
        parent_task_id=parent_id, child_task_id=child_id, approver="alice"
    )
    service.approve(task_id=child_id, plan_hash=child_hash, approver="alice")
    outcome = service.execute(task_id=child_id, plan_hash=child_hash)
    assert outcome.task.status == TaskStatus.READY_TO_PUBLISH


def test_dependency_gate_allows_after_parent_completes(tmp_path):
    service, _ = _service(tmp_path)
    parent_id, parent_hash = _make_task(service, "owner/parent", 1)
    child_id, child_hash = _make_task(service, "owner/child", 2)
    service.attach_child(
        parent_task_id=parent_id, child_task_id=child_id, approver="alice"
    )
    # Complete the parent end-to-end (publish is simulated: no publisher
    # in the fake flow; the gate accepts PR_CREATED and beyond).
    service.approve(task_id=parent_id, plan_hash=parent_hash, approver="alice")
    service.execute(task_id=parent_id, plan_hash=parent_hash)
    service._repo.transition_task(
        parent_id, TaskStatus.PR_CREATED, reason="test publish",
        now="2026-09-17T09:10:00Z",
    )
    # Now the child may execute (fake adapter path: no publisher needed).
    service.approve(task_id=child_id, plan_hash=child_hash, approver="alice")
    outcome = service.execute(task_id=child_id, plan_hash=child_hash)
    assert outcome.task.status == TaskStatus.READY_TO_PUBLISH


def test_parent_status_projection(tmp_path):
    service, _ = _service(tmp_path)
    parent_id, _parent_hash = _make_task(service, "owner/parent", 1)
    c1, c1_hash = _make_task(service, "owner/child-a", 2)
    c2, c2_hash = _make_task(service, "owner/child-b", 3)
    service.attach_child(parent_task_id=parent_id, child_task_id=c1, approver="alice")
    service.attach_child(parent_task_id=parent_id, child_task_id=c2, approver="alice")

    # Nothing started (planned only).
    view = service.parent_status(parent_task_id=parent_id)
    assert view.projected_status == "NOT_STARTED"
    assert view.all_children_complete is False

    # One child completes end-to-end (fake execute succeeds immediately).
    service.approve(task_id=c1, plan_hash=c1_hash, approver="alice")
    service.execute(task_id=c1, plan_hash=c1_hash)
    view = service.parent_status(parent_task_id=parent_id)
    assert view.projected_status == "IN_PROGRESS"
    assert view.all_children_complete is False

    # Second child completes too -> all done.
    service.approve(task_id=c2, plan_hash=c2_hash, approver="alice")
    service.execute(task_id=c2, plan_hash=c2_hash)
    view = service.parent_status(parent_task_id=parent_id)
    assert view.all_children_complete is True
    assert view.projected_status == "COMPLETED"


def test_parent_status_without_children(tmp_path):
    service, _ = _service(tmp_path)
    lone, _ = _make_task(service, "owner/lone", 1)
    with pytest.raises(NotFoundError, match="no children"):
        service.parent_status(parent_task_id=lone)


def test_sibling_failure_does_not_duplicate_sibling_work(tmp_path):
    """片方の失敗・再実行・再承認がもう一方を重複実行させない."""
    service, _ = _service(tmp_path)
    # Enable review queuing on the fake adapter.
    from tests.contract.test_review_flow import ReviewScriptedAdapter

    service._adapter.__class__ = ReviewScriptedAdapter
    service._adapter.review_calls = []
    service._adapter._review_result = None
    service._adapter.execute_calls = 0

    def _count_execute(job):
        service._adapter.execute_calls += 1
        return FakeAgentAdapter.execute(service._adapter, job)

    service._adapter.execute = _count_execute
    parent_id, parent_hash = _make_task(service, "owner/parent", 1)
    c1, c1_hash = _make_task(service, "owner/child-a", 2)
    c2, c2_hash = _make_task(service, "owner/child-b", 3)
    service.attach_child(parent_task_id=parent_id, child_task_id=c1, approver="alice")
    service.attach_child(parent_task_id=parent_id, child_task_id=c2, approver="alice")

    # Both children execute (independent repos, no ordering between them).
    service.approve(task_id=c1, plan_hash=c1_hash, approver="alice")
    service.execute(task_id=c1, plan_hash=c1_hash)
    service.approve(task_id=c2, plan_hash=c2_hash, approver="alice")
    service.execute(task_id=c2, plan_hash=c2_hash)

    # c1's review finds a defect -> c1 goes to NEEDS_REAPPROVAL -> re-execute.
    from agent_platform.domain.models import ReviewResult

    adapter = service._adapter
    adapter.queue_review(
        ReviewResult(
            task_id=c1, repository="owner/child-a", plan_hash=c1_hash,
            base_sha=BASE_SHA, head_sha="e" * 40,
            summary="impl bug",
            findings=[{
                "severity": "medium", "path": "docs/x.md",
                "message": "impl bug", "evidence": "+x",
            }],
        )
    )
    service._repo.transition_task(
        c1, TaskStatus.PR_CREATED, reason="test publish",
        now="2026-09-17T09:08:00Z",
    )
    service.review(
        task_id=c1, plan_hash=c1_hash,
        diff="diff --git a/docs/x.md b/docs/x.md\n+x", head_sha="e" * 40,
    )
    assert service.status(task_id=c1).task.status == TaskStatus.NEEDS_REAPPROVAL

    # Re-execute c1: c2's execution count must not change.
    execute_calls_before = adapter.execute_calls
    service.reexecute(task_id=c1, plan_hash=c1_hash, requested_by="bob")
    assert adapter.execute_calls == execute_calls_before + 1

    # c2 was never re-executed: its execution artifact is unchanged.
    c2_view = service.status(task_id=c2)
    assert c2_view.task.status == TaskStatus.READY_TO_PUBLISH
    assert c2_view.execution is not None


def test_three_task_cycle_detected(tmp_path):
    """A longer cycle (a->b->c->a) is also detected."""
    service, _ = _service(tmp_path)
    a, _ = _make_task(service, "owner/repo-a", 1)
    b, _ = _make_task(service, "owner/repo-b", 2)
    c, _ = _make_task(service, "owner/repo-c", 3)
    service.attach_child(parent_task_id=a, child_task_id=b, approver="alice")
    service.attach_child(parent_task_id=b, child_task_id=c, approver="alice")
    # c -> a closes the 3-cycle.
    with pytest.raises(StateConflictError, match="cycle"):
        service.attach_child(parent_task_id=c, child_task_id=a, approver="alice")


def test_diamond_shape_allowed(tmp_path):
    """Diamond (a->b, a->c, b->d, c->d) is NOT a cycle — shared dependencies
    are legitimate."""
    service, repo = _service(tmp_path)
    a, _ = _make_task(service, "owner/repo-a", 1)
    b, _ = _make_task(service, "owner/repo-b", 2)
    c, _ = _make_task(service, "owner/repo-c", 3)
    d, _ = _make_task(service, "owner/repo-d", 4)
    service.attach_child(parent_task_id=a, child_task_id=b, approver="alice")
    service.attach_child(parent_task_id=a, child_task_id=c, approver="alice")
    service.attach_child(parent_task_id=b, child_task_id=d, approver="alice")
    service.attach_child(parent_task_id=c, child_task_id=d, approver="alice")
