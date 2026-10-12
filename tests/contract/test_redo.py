"""Redo: revision+1 tasks from COMPLETED predecessors; feedback joins
the plan context as untrusted data."""

import pytest

from agent_platform.domain.models import TaskStatus
from agent_platform.errors import StateConflictError
from tests.conftest import BASE_SHA


def _plan(service, repo="owner/repo", issue=1, **kwargs):
    return service.plan(
        repository=repo, issue_number=issue, base_branch="main",
        base_sha=BASE_SHA,
        issue_title=kwargs.get("issue_title", "Build a tiny game"),
        issue_body=kwargs.get("issue_body", "The game must compile."),
        **{k: v for k, v in kwargs.items() if k.startswith("issue_") is False},
    )


def _full_lifecycle(service, task_id, plan_hash):
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="tester")
    service.execute(task_id=task_id, plan_hash=plan_hash)
    fixture_patch = (
        "diff --git a/docs/issues/issue-0001.md"
        " b/docs/issues/issue-0001.md\n"
        "--- a/docs/issues/issue-0001.md\n"
        "+++ b/docs/issues/issue-0001.md\n"
        "@@\n+content\n"
    )
    service.publish(
        task_id=task_id, plan_hash=plan_hash,
        patch=fixture_patch,
    )
    service.review(
        task_id=task_id, plan_hash=plan_hash,
        diff=fixture_patch,
        head_sha="b" * 40,
    )


def test_redo_requires_a_previous_task(repository, artifacts, clock, settings):
    service = _make_service(repository, artifacts, clock, settings)
    with pytest.raises(StateConflictError, match="no previous task"):
        service.redo_plan(
            repository="owner/repo", issue_number=1, base_branch="main",
            base_sha=BASE_SHA,
        )


def test_redo_requires_completed_predecessor(repository, artifacts, clock, settings):
    service = _make_service(repository, artifacts, clock, settings)
    outcome = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA,
        issue_title="Build", issue_body="compile.",
    )
    with pytest.raises(StateConflictError, match="not COMPLETED"):
        service.redo_plan(
            repository="owner/repo", issue_number=1, base_branch="main",
            base_sha=BASE_SHA,
        )
    assert outcome.task.status is TaskStatus.AWAITING_APPROVAL


def test_redo_creates_new_revision_with_feedback(repository, artifacts, clock, settings):
    service = _make_service(repository, artifacts, clock, settings)
    first = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA,
        issue_title="Build", issue_body="compile.",
    )
    task_id, plan_hash = first.task.task_id, first.task.plan_hash
    _full_lifecycle(service, task_id, plan_hash)
    assert repository.get_task(task_id).status is TaskStatus.COMPLETED

    second = service.redo_plan(
        repository="owner/repo", issue_number=1, base_branch="main",
        base_sha=BASE_SHA,
        issue_title="Build", issue_body="compile.",
        feedback="score breaks when invaders wrap",
    )
    assert second.task.task_id != task_id
    assert second.task.revision == 2
    assert second.task.status is TaskStatus.AWAITING_APPROVAL
    assert not second.duplicate
    # Feedback text is in the plan context (the fake adapter's summary does
    # not include it, but the adapter received the job — verify via recording).
    adapter_plan = getattr(service._adapter, "last_plan_job", None)
    assert adapter_plan is not None
    assert "score breaks when invaders wrap" in (adapter_plan.issue_body or "")


def test_redo_explicit_wrong_predecessor_rejected(repository, artifacts, clock, settings):
    service = _make_service(repository, artifacts, clock, settings)
    first = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA,
        issue_title="Build", issue_body="compile.",
    )
    _full_lifecycle(service, first.task.task_id, first.task.plan_hash)
    with pytest.raises(StateConflictError, match="does not belong"):
        service.redo_plan(
            repository="owner/repo", issue_number=1, base_branch="main",
            base_sha=BASE_SHA, predecessor_task_id="task-zzzzzzzzzzzz",
        )


def _make_service(repository, artifacts, clock, settings):
    from agent_platform.adapters.agent.fake import FakeAgentAdapter
    from agent_platform.domain.models import PublishResult
    from agent_platform.orchestrator.service import TaskService

    class RecordingFakeAdapter(FakeAgentAdapter):
        last_plan_job = None

        def plan(self, job):
            self.last_plan_job = job
            return super().plan(job)

    class StubPublisher:
        """Minimal publish double: the redo tests only need the task to
        reach PR_CREATED so review can run."""

        def publish(self, request):
            return PublishResult(
                task_id=request.task_id,
                plan_hash=request.plan_hash,
                duplicate=False,
                pr_number=1,
                pr_url="https://example.invalid/pr/1",
                branch_name=f"agent-platform/{request.task_id}",
            )

    return TaskService(
        repository=repository,
        adapter=RecordingFakeAdapter(),
        artifacts=artifacts,
        clock=clock,
        settings=settings,
        publisher=StubPublisher(),
    )
