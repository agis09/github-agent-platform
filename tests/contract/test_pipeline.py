"""Post-approval pipeline (README「処理の流れ」): approve -> execute ->
publish -> review without further commands.

- stage routing: continue only through READY_TO_PUBLISH / PR_CREATED
- a stop or an exception is reported (notify + report), never swallowed
- the approval gate is unchanged: the pipeline cannot execute an
  unapproved task (TaskService re-checks the approval)
- the runner is serial and drains its queue on close
"""

from agent_platform.domain.models import PublishResult, TaskStatus
from agent_platform.orchestrator.pipeline import PipelineRunner, run_pipeline
from tests.conftest import BASE_SHA

TASK = "task-0123456789ab"
HASH = "sha256:" + "a" * 64


class ScriptedSteps:
    def __init__(self, execute=TaskStatus.READY_TO_PUBLISH,
                 publish=(TaskStatus.PR_CREATED, 7), review=TaskStatus.COMPLETED):
        self._execute, self._publish, self._review = execute, publish, review
        self.calls: list[str] = []
        self.notices: list[tuple[str, str, str]] = []

    def _result(self, value):
        if isinstance(value, Exception):
            raise value
        return value

    def execute(self, task_id, plan_hash):
        self.calls.append(f"execute:{task_id}")
        return self._result(self._execute)

    def publish(self, task_id, plan_hash):
        self.calls.append(f"publish:{task_id}")
        return self._result(self._publish)

    def review(self, task_id, plan_hash, pr_number):
        self.calls.append(f"review:{task_id}:#{pr_number}")
        return self._result(self._review)

    def notify_stopped(self, task_id, stage, status):
        self.notices.append((task_id, stage, status))


def test_full_run_reaches_review_without_notice():
    steps = ScriptedSteps()
    report = run_pipeline(steps, TASK, HASH, review_enabled=True)
    assert steps.calls == [f"execute:{TASK}", f"publish:{TASK}", f"review:{TASK}:#7"]
    assert report.stages == [
        ("execute", "READY_TO_PUBLISH"), ("publish", "PR_CREATED"), ("review", "COMPLETED"),
    ]
    assert report.stopped_at is None
    assert report.pr_number == 7
    assert steps.notices == []


def test_review_with_findings_still_counts_as_done():
    # REVIEWING (non-blocking findings recorded) is a normal end state.
    steps = ScriptedSteps(review=TaskStatus.REVIEWING)
    report = run_pipeline(steps, TASK, HASH, review_enabled=True)
    assert report.stopped_at is None
    assert steps.notices == []


def test_violations_stop_before_publish():
    steps = ScriptedSteps(execute=TaskStatus.NEEDS_REAPPROVAL)
    report = run_pipeline(steps, TASK, HASH, review_enabled=True)
    assert steps.calls == [f"execute:{TASK}"]
    assert report.stopped_at == "execute"
    assert steps.notices == [(TASK, "execute", "NEEDS_REAPPROVAL")]


def test_blocking_review_is_reported():
    steps = ScriptedSteps(review=TaskStatus.NEEDS_REAPPROVAL)
    report = run_pipeline(steps, TASK, HASH, review_enabled=True)
    assert report.stopped_at == "review"
    assert steps.notices == [(TASK, "review", "NEEDS_REAPPROVAL")]


def test_review_disabled_ends_after_publish():
    steps = ScriptedSteps()
    report = run_pipeline(steps, TASK, HASH, review_enabled=False)
    assert steps.calls == [f"execute:{TASK}", f"publish:{TASK}"]
    assert report.stopped_at is None
    assert steps.notices == []


def test_exception_is_reported_with_redaction(monkeypatch):
    canary = "canary-secret-7f3a9c"
    monkeypatch.setenv("AGENT_PLATFORM_PUBLISHER_TOKEN", canary)
    steps = ScriptedSteps(publish=RuntimeError(f"push failed with token {canary}"))
    report = run_pipeline(steps, TASK, HASH, review_enabled=True)
    assert report.stopped_at == "publish"
    assert steps.notices == [(TASK, "publish", "error")]
    assert report.error is not None
    assert report.error.startswith("RuntimeError: push failed")
    assert canary not in report.error


def test_runner_is_serial_and_drains_on_close():
    steps = ScriptedSteps()
    lines: list[str] = []
    runner = PipelineRunner(steps, review_enabled=False, log=lines.append)
    runner.submit("task-000000000001", HASH)
    runner.submit("task-000000000002", HASH)
    runner.close(timeout=5)
    assert steps.calls == [
        "execute:task-000000000001", "publish:task-000000000001",
        "execute:task-000000000002", "publish:task-000000000002",
    ]
    assert [line.split()[1] for line in lines] == [
        "queued", "queued", "start", "done", "start", "done",
    ]


# --- through the real TaskService: the approval gate is unchanged ---


class _ServiceSteps:
    """Steps over a real TaskService (fake adapter + stub publisher)."""

    def __init__(self, service):
        self.service = service
        self.notices: list[tuple[str, str, str]] = []

    def execute(self, task_id, plan_hash):
        return self.service.execute(task_id=task_id, plan_hash=plan_hash).task.status

    def publish(self, task_id, plan_hash):
        patch = (
            "diff --git a/docs/issues/issue-0001.md b/docs/issues/issue-0001.md\n"
            "--- a/docs/issues/issue-0001.md\n+++ b/docs/issues/issue-0001.md\n"
            "@@\n+content\n"
        )
        outcome = self.service.publish(task_id=task_id, plan_hash=plan_hash, patch=patch)
        return outcome.task.status, outcome.pr_number

    def review(self, task_id, plan_hash, pr_number):
        raise AssertionError("review not enabled in this test")

    def notify_stopped(self, task_id, stage, status):
        self.notices.append((task_id, stage, status))


class _StubPublisher:
    def publish(self, request):
        return PublishResult(
            task_id=request.task_id, plan_hash=request.plan_hash, duplicate=False,
            pr_number=5, pr_url="https://example.invalid/pr/5",
            branch_name=f"agent-platform/{request.task_id}",
        )


def _service(repository, artifacts, clock, settings):
    from agent_platform.adapters.agent.fake import FakeAgentAdapter
    from agent_platform.orchestrator.service import TaskService

    return TaskService(
        repository=repository, adapter=FakeAgentAdapter(), artifacts=artifacts,
        clock=clock, settings=settings, publisher=_StubPublisher(),
    )


def _plan(service):
    return service.plan(
        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA,
    )


def test_approved_task_runs_to_draft_pr(repository, artifacts, clock, settings):
    service = _service(repository, artifacts, clock, settings)
    plan = _plan(service)
    task_id, plan_hash = plan.task.task_id, plan.task.plan_hash
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="alice")

    steps = _ServiceSteps(service)
    report = run_pipeline(steps, task_id, plan_hash, review_enabled=False)
    assert report.stopped_at is None, report.error
    assert report.pr_number == 5
    assert repository.get_task(task_id).status is TaskStatus.PR_CREATED


def test_unapproved_task_is_refused_by_the_gate(repository, artifacts, clock, settings):
    service = _service(repository, artifacts, clock, settings)
    plan = _plan(service)
    task_id, plan_hash = plan.task.task_id, plan.task.plan_hash

    steps = _ServiceSteps(service)
    report = run_pipeline(steps, task_id, plan_hash, review_enabled=False)
    assert report.stopped_at == "execute"
    assert steps.notices == [(task_id, "execute", "error")]
    # Nothing ran: the task is still waiting for a human.
    assert repository.get_task(task_id).status is TaskStatus.AWAITING_APPROVAL
    assert repository.get_task(task_id).execution_ref is None


def test_publish_reports_the_draft_pr_on_the_issue(
    repository, artifacts, clock, settings, monkeypatch,
):
    from agent_platform import cli

    class _Reporter:
        def __init__(self):
            self.pr_notices = []

        def post_plan_comment(self, **kwargs):
            return 1

        def post_pr_created(self, **kwargs):
            self.pr_notices.append(kwargs)
            return 2

    rec = _Reporter()
    monkeypatch.setattr(cli, "_build_reporter", lambda: rec)
    service = _service(repository, artifacts, clock, settings)
    plan = _plan(service)
    task_id, plan_hash = plan.task.task_id, plan.task.plan_hash
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="alice")
    service.execute(task_id=task_id, plan_hash=plan_hash)

    patch = (
        "diff --git a/docs/issues/issue-0001.md b/docs/issues/issue-0001.md\n"
        "--- a/docs/issues/issue-0001.md\n+++ b/docs/issues/issue-0001.md\n"
        "@@\n+content\n"
    )
    outcome = cli._publish_task(service, task_id, plan_hash, patch)
    assert outcome.pr_number == 5
    assert rec.pr_notices == [{
        "repository": "owner/repo", "issue_number": 1,
        "task_id": task_id, "pr_number": 5,
    }]
