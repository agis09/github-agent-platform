"""Contract: publish flow through TaskService with a fake publisher.

Covers: READY_TO_PUBLISH gate, patch digest/changed files consistency,
approval re-check, idempotent publish (no duplicate PR), and rejection of
unapproved/tampered patches.
"""

import datetime

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.adapters.kubernetes.models import JobLaunchSpec  # noqa: F401
from agent_platform.clock import FixedClock
from agent_platform.config import Settings
from agent_platform.domain.models import (
    TaskStatus,
)
from agent_platform.errors import (
    PlanHashMismatchError,
    StateConflictError,
)
from agent_platform.orchestrator.repository import SqliteTaskRepository
from agent_platform.orchestrator.service import TaskService
from agent_platform.publisher.publisher import PullRequestPublisher
from tests.conftest import BASE_SHA

UTC = datetime.UTC


class FakePublisher:
    """Records publish calls; returns canned results."""

    def __init__(self) -> None:
        self.requests = []
        self.existing_pr: dict | None = None

    def publish(self, request):
        self.requests.append(request)
        if self.existing_pr is not None:
            from agent_platform.domain.models import PublishResult

            return PublishResult(
                task_id=request.task_id,
                plan_hash=request.plan_hash,
                branch_name=f"agent-platform/{request.task_id}",
                pr_number=42,
                pr_url="https://example/pr/42",
                duplicate=True,
            )
        from agent_platform.domain.models import PublishResult

        return PublishResult(
            task_id=request.task_id,
            plan_hash=request.plan_hash,
            branch_name=f"agent-platform/{request.task_id}",
            pr_number=7,
            pr_url="https://example/pr/7",
            duplicate=False,
        )


def _service(tmp_path, clock, publisher):
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
        publisher=publisher,
    ), repo


def _ready_task(service, issue=1):
    outcome = service.plan(
        repository="owner/repo", issue_number=issue, base_branch="main",
        base_sha=BASE_SHA,
    )
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="alice")
    # fake adapter execute succeeds with changed_files = allowed paths
    outcome_exec = service.execute(task_id=task_id, plan_hash=plan_hash)
    assert outcome_exec.task.status == TaskStatus.READY_TO_PUBLISH
    return task_id, plan_hash


def test_publish_creates_pr_and_records_idempotency(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    publisher = FakePublisher()
    service, _ = _service(tmp_path, clock, publisher)
    task_id, plan_hash = _ready_task(service)
    patch = "diff --git a/docs/issues/issue-0001.md b/docs/issues/issue-0001.md\n+content"

    outcome = service.publish(task_id=task_id, plan_hash=plan_hash, patch=patch)
    assert outcome.duplicate is False
    assert outcome.pr_number == 7
    assert outcome.task.status == TaskStatus.PR_CREATED
    assert len(publisher.requests) == 1
    request = publisher.requests[0]
    assert request.approver == "alice"
    assert request.repository == "owner/repo"
    assert request.base_sha == BASE_SHA

    # Idempotent replay: second publish returns duplicate, no second PR call.
    outcome2 = service.publish(task_id=task_id, plan_hash=plan_hash, patch=patch)
    assert outcome2.duplicate is True
    assert len(publisher.requests) == 1


def test_publish_refused_before_ready(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    publisher = FakePublisher()
    service, _ = _service(tmp_path, clock, publisher)
    outcome = service.plan(
        repository="owner/repo", issue_number=2, base_branch="main", base_sha=BASE_SHA
    )
    with pytest.raises(StateConflictError, match="not READY_TO_PUBLISH"):
        service.publish(
            task_id=outcome.task.task_id,
            plan_hash=outcome.task.plan_hash,
            patch="diff",
        )
    assert publisher.requests == []


def test_publish_refused_wrong_hash(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    publisher = FakePublisher()
    service, _ = _service(tmp_path, clock, publisher)
    task_id, plan_hash = _ready_task(service)
    with pytest.raises(PlanHashMismatchError):
        service.publish(
            task_id=task_id, plan_hash="sha256:" + "0" * 64,
            patch="diff",
        )
    assert publisher.requests == []


def test_publish_refused_out_of_scope_patch(tmp_path):
    """改ざんpatch（allowlist外の変更）は拒否."""
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    publisher = FakePublisher()
    service, _ = _service(tmp_path, clock, publisher)
    task_id, plan_hash = _ready_task(service)
    tampered = "diff --git a/src/evil.py b/src/evil.py\n+import os; os.system('x')"
    from agent_platform.publisher.git_ops import PatchRejectedError

    with pytest.raises(
        PatchRejectedError,
        match="outside the allowed paths|changed_files disagreement",
    ):
        service.publish(task_id=task_id, plan_hash=plan_hash, patch=tampered)
    assert publisher.requests == []


def test_publish_without_publisher_configured(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, _ = _service(tmp_path, clock, None)
    task_id, plan_hash = _ready_task(service)
    with pytest.raises(StateConflictError, match="no publisher configured"):
        service.publish(
            task_id=task_id, plan_hash=plan_hash, patch="diff"
        )


def test_publisher_branch_name_deterministic(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    publisher = FakePublisher()
    service, _ = _service(tmp_path, clock, publisher)
    task_id, plan_hash = _ready_task(service)
    service.publish(
        task_id=task_id, plan_hash=plan_hash,
        patch="diff --git a/docs/issues/issue-0001.md b/docs/issues/issue-0001.md\n+x",
    )
    assert publisher.requests[0].task_id == task_id
    assert PullRequestPublisher.branch_name(task_id) == f"agent-platform/{task_id}"
