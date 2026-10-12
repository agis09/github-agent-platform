"""Contract: Execute Job flow.

Proof obligations:
- no Job is launched on a plan hash / base SHA mismatch (adapter receives
  nothing when the gate refuses)
- a detected violation never advances to READY_TO_PUBLISH
- every command, exit code and changed file is auditable
- a duplicate execute from the same approval is an idempotent replay
- the approval gate runs before launch
"""

import datetime

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.clock import FixedClock
from agent_platform.config import Settings
from agent_platform.domain.models import (
    ExecuteJobSpec,
    ExecutionResult,
    TaskStatus,
    Violation,
)
from agent_platform.errors import (
    ExpiredApprovalError,
    PlanHashMismatchError,
)
from agent_platform.orchestrator.repository import SqliteTaskRepository
from agent_platform.orchestrator.service import TaskService
from tests.conftest import BASE_SHA

UTC = datetime.UTC


class ScriptedAdapter(FakeAgentAdapter):
    """FakeAdapter with an overridable execute() for violation scenarios."""

    def __init__(self) -> None:
        super().__init__()
        self.execute_calls: list[ExecuteJobSpec] = []
        self._execute_result: ExecutionResult | None = None
        self._execute_exc: Exception | None = None

    def queue_execute(self, result: ExecutionResult | None = None,
                      exc: Exception | None = None) -> None:
        self._execute_result = result
        self._execute_exc = exc

    def execute(self, job: ExecuteJobSpec) -> ExecutionResult:
        self.execute_calls.append(job)
        if self._execute_exc is not None:
            raise self._execute_exc
        assert self._execute_result is not None
        return self._execute_result


def _service(tmp_path, clock):
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
    adapter = ScriptedAdapter()
    repo = SqliteTaskRepository(tmp_path / "state.sqlite3")
    service = TaskService(
        repository=repo,
        adapter=adapter,
        artifacts=LocalFsArtifactStore(tmp_path / "artifacts", 1024 * 1024),
        clock=clock,
        settings=settings,
    )
    return service, adapter, repo


def _plan_and_approve(service, issue=1, approver="alice"):
    outcome = service.plan(
        repository="owner/repo", issue_number=issue, base_branch="main",
        base_sha=BASE_SHA,
    )
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    service.approve(task_id=task_id, plan_hash=plan_hash, approver=approver)
    return task_id, plan_hash


def _result(task_id, plan_hash, **overrides) -> ExecutionResult:
    values = {
        "schema_version": "1",
        "task_id": task_id,
        "plan_hash": plan_hash,
        "base_sha": BASE_SHA,
        "status": "succeeded",
        "changed_files": ["docs/usage.md"],
        "commands": [],
        "tests": [],
        "violations": [],
        "pr_body_draft": "draft",
    }
    values.update(overrides)
    return ExecutionResult.model_validate(values)


def test_execute_happy_path_reaches_ready_to_publish(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _plan_and_approve(service)
    adapter.queue_execute(_result(task_id, plan_hash))

    outcome = service.execute(task_id=task_id, plan_hash=plan_hash)
    assert outcome.duplicate is False
    assert outcome.task.status == TaskStatus.READY_TO_PUBLISH
    assert outcome.execution is not None
    assert outcome.execution.changed_files == ["docs/usage.md"]
    assert len(adapter.execute_calls) == 1
    # The spec the adapter received is pinned to the approval's frozen values.
    job = adapter.execute_calls[0]
    assert job.base_sha == BASE_SHA
    assert job.plan_hash == plan_hash
    assert job.plan_ref.startswith("artifact://")


def test_execute_refused_without_approval_no_job_launched(tmp_path):
    """plan hash 不一致時に Job を起動しない / 承認なしで Job を起動しない."""
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    outcome = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA
    )
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    adapter.queue_execute(_result(task_id, plan_hash))

    with pytest.raises(ExpiredApprovalError, match="no approval record"):
        service.execute(task_id=task_id, plan_hash=plan_hash)
    assert adapter.execute_calls == []  # nothing launched

    # Wrong hash also refuses before launch.
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="alice")
    with pytest.raises(PlanHashMismatchError):
        service.execute(task_id=task_id, plan_hash="sha256:" + "0" * 64)
    assert adapter.execute_calls == []


def test_execute_refused_on_base_sha_mismatch(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _plan_and_approve(service)
    adapter.queue_execute(_result(task_id, plan_hash))

    other_sha = "b" * 40
    with pytest.raises(PlanHashMismatchError, match="base_sha mismatch"):
        service.execute(task_id=task_id, plan_hash=plan_hash, base_sha=other_sha)
    assert adapter.execute_calls == []


def test_execute_duplicate_replays_without_relaunch(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _plan_and_approve(service)
    adapter.queue_execute(_result(task_id, plan_hash))

    first = service.execute(task_id=task_id, plan_hash=plan_hash)
    second = service.execute(task_id=task_id, plan_hash=plan_hash)
    assert first.duplicate is False
    assert second.duplicate is True
    assert len(adapter.execute_calls) == 1
    assert second.task.status == TaskStatus.READY_TO_PUBLISH


def test_violations_block_ready_to_publish(tmp_path):
    """違反検知時は READY_TO_PUBLISH へ進まない."""
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _plan_and_approve(service)
    adapter.queue_execute(
        _result(
            task_id,
            plan_hash,
            violations=[Violation(kind="out_of_scope_path", detail="src/other.py")],
        )
    )
    outcome = service.execute(task_id=task_id, plan_hash=plan_hash)
    assert outcome.task.status == TaskStatus.NEEDS_REAPPROVAL
    assert outcome.execution.violations[0].kind == "out_of_scope_path"


def test_failed_execution_goes_to_failed_state(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _plan_and_approve(service)
    adapter.queue_execute(_result(task_id, plan_hash, status="failed"))

    outcome = service.execute(task_id=task_id, plan_hash=plan_hash)
    assert outcome.task.status == TaskStatus.FAILED


def test_adapter_exception_fails_task(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _plan_and_approve(service)
    adapter.queue_execute(exc=RuntimeError("sandbox exploded"))

    with pytest.raises(RuntimeError):
        service.execute(task_id=task_id, plan_hash=plan_hash)
    assert service.status(task_id=task_id).task.status == TaskStatus.FAILED


def test_execution_artifact_digest_recorded(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, repo = _service(tmp_path, clock)
    task_id, plan_hash = _plan_and_approve(service)
    adapter.queue_execute(_result(task_id, plan_hash))
    service.execute(task_id=task_id, plan_hash=plan_hash)

    task = repo.get_task(task_id)
    assert task.execution_ref is not None
    digest = task.execution_ref.removeprefix("artifact://")
    meta = repo.get_artifact_metadata(digest)
    assert meta is not None
    assert meta["retention_class"] == "audit"
    # The stored artifact round-trips.
    loaded = ExecutionResult.model_validate_json(
        LocalFsArtifactStore(tmp_path / "artifacts", 1024 * 1024)
        .get(type("R", (), {"uri": task.execution_ref, "digest": digest})()).data
    )
    assert loaded.status == "succeeded"


def test_status_view_includes_execution(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _plan_and_approve(service)
    adapter.queue_execute(_result(task_id, plan_hash))
    service.execute(task_id=task_id, plan_hash=plan_hash)

    view = service.status(task_id=task_id)
    assert view.execution is not None
    assert view.execution.status == "succeeded"
    events = view.events
    assert any(e["to"] == "EXECUTING" for e in events)
    assert any(e["to"] == "READY_TO_PUBLISH" for e in events)
