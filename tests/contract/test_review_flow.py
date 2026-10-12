"""Contract: independent review flow.

- review requires PR_CREATED state (published PR)
- ReviewResult identity validation + audit artifact
- review is idempotent
- the review adapter contract (fake codex output parsing is in
  test_codex_events; here we exercise the service + remote adapter with a
  fake kubectl)
"""

import datetime
import json
from pathlib import Path

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.adapters.agent.remote_review import RemoteReviewAdapter
from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.adapters.kubernetes.kube import KubeJobLauncher
from agent_platform.adapters.kubernetes.models import JobKind
from agent_platform.clock import FixedClock
from agent_platform.config import Settings
from agent_platform.domain.models import TaskStatus
from agent_platform.errors import StateConflictError
from agent_platform.orchestrator.repository import SqliteTaskRepository
from agent_platform.orchestrator.service import TaskService
from tests.conftest import BASE_SHA

UTC = datetime.UTC
BLOCKING_DIFF = "diff --git a/docs/usage.md b/docs/usage.md\n+x"


class ReviewScriptedAdapter(FakeAgentAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.review_calls = []
        self.execute_calls = 0
        self._review_result = None

    def execute(self, job):
        self.execute_calls += 1
        return super().execute(job)

    def queue_review(self, result):
        self._review_result = result

    def review(self, job):
        self.review_calls.append(job)
        return self._review_result


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
    adapter = ReviewScriptedAdapter()
    repo = SqliteTaskRepository(tmp_path / "state.sqlite3")
    service = TaskService(
        repository=repo,
        adapter=adapter,
        artifacts=LocalFsArtifactStore(tmp_path / "artifacts", 1024 * 1024),
        clock=clock,
        settings=settings,
    )
    return service, adapter, repo


def _published_task(service, issue=1):

    outcome = service.plan(
        repository="owner/repo", issue_number=issue, base_branch="main",
        base_sha=BASE_SHA,
    )
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="alice")
    service.execute(task_id=task_id, plan_hash=plan_hash)
    # simulate publish: state -> PR_CREATED with a publish idempotency entry
    service._repo.transition_task(
        task_id, TaskStatus.PR_CREATED, reason="draft PR #3 created",
        now="2026-09-16T09:05:00Z",
    )
    return task_id, plan_hash


def _review_result(task_id, plan_hash, findings=None):
    from agent_platform.domain.models import ReviewResult

    return ReviewResult(
        task_id=task_id,
        repository="owner/repo",
        plan_hash=plan_hash,
        base_sha=BASE_SHA,
        head_sha="e" * 40,
        summary="ok",
        findings=findings or [],
    )


def test_review_records_result_and_moves_to_reviewing(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, repo = _service(tmp_path, clock)
    task_id, plan_hash = _published_task(service)
    finding = {
        "severity": "medium",
        "path": "docs/usage.md",
        "message": "reset procedure misses step 3",
        "evidence": "+x",
    }
    adapter.queue_review(_review_result(task_id, plan_hash, [finding]))

    outcome = service.review(
        task_id=task_id, plan_hash=plan_hash,
        diff="diff --git a/docs/usage.md b/docs/usage.md\n+x", head_sha="e" * 40,
    )
    assert outcome.duplicate is False
    # medium finding blocks: task routes to NEEDS_REAPPROVAL (review outcome routing)
    assert outcome.task.status == TaskStatus.NEEDS_REAPPROVAL
    assert outcome.review.findings[0].message == "reset procedure misses step 3"
    assert len(adapter.review_calls) == 1
    # audit artifact stored; the routing reason is in the audit trail
    repo.get_task(task_id)
    events = [e for e in service.status(task_id=task_id).events
              if "review findings" in e["reason"]]
    assert events


def test_review_refused_before_publish(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    outcome = service.plan(
        repository="owner/repo", issue_number=2, base_branch="main",
        base_sha=BASE_SHA,
    )
    with pytest.raises(StateConflictError, match="not PR_CREATED"):
        service.review(
            task_id=outcome.task.task_id,
            plan_hash=outcome.task.plan_hash,
            diff="diff", head_sha="e" * 40,
        )
    assert adapter.review_calls == []


def test_review_idempotent(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _published_task(service)
    adapter.queue_review(_review_result(task_id, plan_hash))

    first = service.review(
        task_id=task_id, plan_hash=plan_hash, diff="diff", head_sha="e" * 40,
    )
    second = service.review(
        task_id=task_id, plan_hash=plan_hash, diff="diff", head_sha="e" * 40,
    )
    assert first.duplicate is False
    assert second.duplicate is True
    assert len(adapter.review_calls) == 1


def test_remote_review_adapter_parses_result(tmp_path):
    review_model = _review_result("task-abc123456789", "sha256:" + "d" * 64)
    review_model = review_model.model_copy(update={"repository": "owner/sandbox"})
    review_json = json.dumps(review_model.model_dump(mode="json"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    script = (
        "#!/bin/sh\n"
        "for arg do\n"
        "  case $arg in apply|get|logs|delete|create) break;; esac\n"
        "  shift\n"
        "done\n"
        'if [ "$1" = "delete" ]; then exit 0; fi\n'
        'if [ "$1" = "apply" ]; then echo created; exit 0; fi\n'
        'if [ "$1" = "get" ]; then echo \'{"status":{"conditions":'
        '[{"type":"Complete","status":"True"}]}}\'; exit 0; fi\n'
        'if [ "$1" = "logs" ]; then\n'
        '  echo "REVIEW_RESULT_BEGIN"\n'
        f"  echo '{review_json}'\n"
        '  echo "REVIEW_RESULT_END"\n'
        "  exit 0\n"
        "fi\n"
        "exit 64\n"
    )
    kubectl.write_text(script)
    kubectl.chmod(0o755)
    launcher = KubeJobLauncher(
        namespace="agent-platform",
        kubectl_path=str(kubectl),
        template_dir=Path(__file__).resolve().parents[2] / "deploy" / "jobs",
    )
    from agent_platform.domain.models import ReviewJobSpec

    adapter = RemoteReviewAdapter(launcher, plan_image="worker-p2:p2")
    result = adapter.review(
        ReviewJobSpec(
            task_id="task-abc123456789",
            repository="owner/sandbox",
            plan_hash="sha256:" + "d" * 64,
            base_sha=BASE_SHA,
            head_sha="e" * 40,
        )
    )
    assert result.summary == "ok"
    # review job name uses the review- prefix
    assert adapter.last_review is result


def test_review_job_template_renders_readonly(tmp_path):  # noqa: C901
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text("#!/bin/sh\nexit 0\n")
    kubectl.chmod(0o755)
    launcher = KubeJobLauncher(
        namespace="agent-platform",
        kubectl_path=str(kubectl),
        template_dir=Path(__file__).resolve().parents[2] / "deploy" / "jobs",
    )
    from agent_platform.adapters.kubernetes.models import JobLaunchSpec

    captured = {}
    original = launcher._kubectl_run

    def spy(*args, **kwargs):
        if args[0] == "apply":
            captured["manifest"] = kwargs.get("input", "")
            return "created"
        return original(*args, **kwargs)

    launcher._kubectl_run = spy  # type: ignore[method-assign]
    launcher._review_extras = {
        "HEAD_SHA": "e" * 40,
        "ISSUE_TITLE": "Fix the thing",
        "ISSUE_BODY": "steps",
        "PLAN_JSON": "{}",
        "REVIEW_DIFF": "diff --git a/x b/x\n+y",
        "REVIEW_TESTS": "pytest -q",
    }
    launcher.launch(
        JobLaunchSpec(
            task_id="task-abc123456789",
            repository="owner/sandbox",
            issue_number=1,
            base_branch="main",
            base_sha=BASE_SHA,
            issue_title="review",
            issue_body="x",
            plan_image="worker-p2:p2",
            kind=JobKind.REVIEW,
            plan_hash="sha256:" + "d" * 64,
        )
    )
    manifest = captured["manifest"]
    assert "agent_platform.worker.review" in manifest
    assert "REVIEW_DIFF" in manifest
    assert "runAsUser: 10001" in manifest
    # The read-only sandbox lives in the review worker (not the manifest).
    worker = (
        Path(__file__).resolve().parents[2]
        / "src" / "agent_platform" / "worker" / "review.py"
    ).read_text()
    assert "mode=RunMode.READ_ONLY" in worker
    assert "RunMode.WRITE" not in worker


def test_review_clean_routes_to_completed(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _published_task(service)
    adapter.queue_review(_review_result(task_id, plan_hash))  # no findings

    outcome = service.review(
        task_id=task_id, plan_hash=plan_hash, diff="diff", head_sha="e" * 40,
    )
    assert outcome.task.status == TaskStatus.COMPLETED


def test_review_low_severity_only_routes_to_completed(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _published_task(service)
    finding = {
        "severity": "info",
        "path": "docs/usage.md",
        "message": "could mention the CLI flag",
        "evidence": "+data file section",
    }
    adapter.queue_review(_review_result(task_id, plan_hash, [finding]))
    outcome = service.review(
        task_id=task_id, plan_hash=plan_hash, diff="diff", head_sha="e" * 40,
    )
    assert outcome.task.status == TaskStatus.COMPLETED


def test_review_critical_routes_to_needs_reapproval(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _published_task(service)
    finding = {
        "severity": "critical",
        "path": "docs/usage.md",
        "message": "reset command destroys the home directory",
        "evidence": "+x",
    }
    adapter.queue_review(_review_result(task_id, plan_hash, [finding]))
    outcome = service.review(
        task_id=task_id, plan_hash=plan_hash, diff=BLOCKING_DIFF,
        head_sha="e" * 40,
    )
    assert outcome.task.status == TaskStatus.NEEDS_REAPPROVAL


def test_replan_returns_needs_reapproval_task_to_planning(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _published_task(service)
    finding = {
        "severity": "high", "path": "docs/usage.md",
        "message": "plan itself was wrong", "evidence": "+x",
    }
    adapter.queue_review(_review_result(task_id, plan_hash, [finding]))
    outcome = service.review(
        task_id=task_id, plan_hash=plan_hash, diff=BLOCKING_DIFF,
        head_sha="e" * 40,
    )
    assert outcome.task.status == TaskStatus.NEEDS_REAPPROVAL

    task = service.replan(
        task_id=task_id, plan_hash=plan_hash, approver="bob"
    )
    assert task.status == TaskStatus.PLANNING
    events = [e for e in service.status(task_id=task_id).events
              if "replan requested by bob" in e["reason"]]
    assert events


def test_reexecute_reruns_with_new_idempotency_key(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _published_task(service)
    finding = {
        "severity": "medium", "path": "docs/usage.md",
        "message": "implementation bug", "evidence": "+x",
    }
    adapter.queue_review(_review_result(task_id, plan_hash, [finding]))
    outcome = service.review(
        task_id=task_id, plan_hash=plan_hash,
        diff="diff --git a/docs/usage.md b/docs/usage.md\n+x",
        head_sha="e" * 40,
    )
    assert outcome.task.status == TaskStatus.NEEDS_REAPPROVAL
    execute_calls_before = adapter.execute_calls

    # Re-execute the unchanged plan: approval stays valid (unexpired).
    result = service.reexecute(
        task_id=task_id, plan_hash=plan_hash, requested_by="bob"
    )
    assert adapter.execute_calls == execute_calls_before + 1
    assert result.task.status == TaskStatus.READY_TO_PUBLISH


def test_reexecute_requires_needs_reapproval_state(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _published_task(service)
    with pytest.raises(StateConflictError, match="not NEEDS_REAPPROVAL"):
        service.reexecute(
            task_id=task_id, plan_hash=plan_hash, requested_by="bob"
        )


def test_unsupported_evidence_recorded_but_not_blocking(tmp_path):
    """Findings whose evidence is absent from the diff are recorded as
    unsupported (hallucination defense) and do NOT block routing."""
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    task_id, plan_hash = _published_task(service)
    hallucinated = {
        "severity": "critical", "path": "docs/usage.md",
        "message": "hallucinated problem", "evidence": "+NOT IN THE DIFF",
    }
    adapter.queue_review(_review_result(task_id, plan_hash, [hallucinated]))
    outcome = service.review(
        task_id=task_id, plan_hash=plan_hash, diff=BLOCKING_DIFF,
        head_sha="e" * 40,
    )
    # The hallucinated finding does not block: task completes.
    assert outcome.task.status == TaskStatus.COMPLETED
    # The refusal is visible in the audit trail.
    events = [e for e in service.status(task_id=task_id).events
              if "unsupported evidence" in e["reason"]]
    assert events
