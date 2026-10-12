"""Recovery paths that replace manual direct SQL transitions:

- redo from a FAILED task creates a new revision (FAILED stays terminal)
- a redo revision is approvable (its plan artifact is stored)
- reopen sends READY_TO_PUBLISH back to NEEDS_REAPPROVAL, after which the
  existing reexecute / replan paths apply
"""

import datetime

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.clock import FixedClock
from agent_platform.domain.models import ArtifactRef, TaskStatus
from agent_platform.errors import (
    AdapterContractError,
    PlanHashMismatchError,
    StateConflictError,
    TaskStateRejectedError,
)
from agent_platform.orchestrator.service import TaskService
from tests.conftest import BASE_SHA
from tests.contract.test_review_flow import _service

UTC = datetime.UTC


class FlakyPlanAdapter(FakeAgentAdapter):
    """Returns an empty proposed_changes plan while `empty` is set — the
    failure mode observed with some models."""

    empty = False
    ask = False  # requires_human_input with a question, no files

    def plan(self, job):
        self.last_job = job
        result = super().plan(job)
        if self.ask:
            return result.model_copy(update={
                "proposed_changes": [], "requires_human_input": True,
                "summary": "max_tokens is not specified",
                "questions": ["Which max_tokens value should the CLI send?"],
            })
        if self.empty:
            return result.model_copy(update={"proposed_changes": []})
        return result


def _flaky_service(repository, artifacts, clock, settings):
    adapter = FlakyPlanAdapter()
    service = TaskService(
        repository=repository, adapter=adapter, artifacts=artifacts,
        clock=clock, settings=settings,
    )
    return service, adapter


def _plan(service, **kwargs):
    return service.plan(
        repository="owner/repo", issue_number=1, base_branch="main",
        base_sha=BASE_SHA, issue_title="Build", issue_body="compile.", **kwargs,
    )


def _redo(service):
    return service.redo_plan(
        repository="owner/repo", issue_number=1, base_branch="main",
        base_sha=BASE_SHA, issue_title="Build", issue_body="compile.",
    )


def test_redo_from_failed_plan_creates_new_revision(repository, artifacts, clock, settings):
    service, adapter = _flaky_service(repository, artifacts, clock, settings)
    adapter.empty = True
    with pytest.raises(AdapterContractError, match="no file changes"):
        _plan(service)
    failed = repository.get_task_by_issue("owner/repo", 1)
    assert failed.status is TaskStatus.FAILED

    # run on the FAILED task stays an idempotent replay (no new task).
    replay = _plan(service)
    assert replay.duplicate
    assert replay.task.task_id == failed.task_id

    adapter.empty = False
    second = _redo(service)
    assert second.task.task_id != failed.task_id
    assert second.task.revision == 2
    assert second.task.status is TaskStatus.AWAITING_APPROVAL
    # The failed attempt is untouched (terminal, still auditable).
    assert repository.get_task(failed.task_id).status is TaskStatus.FAILED


def test_redo_revision_is_approvable_and_executable(repository, artifacts, clock, settings):
    service, adapter = _flaky_service(repository, artifacts, clock, settings)
    adapter.empty = True
    with pytest.raises(AdapterContractError):
        _plan(service)
    adapter.empty = False
    second = _redo(service)
    task_id, plan_hash = second.task.task_id, second.task.plan_hash
    # The plan artifact must exist: approve and execute load it by digest.
    digest = plan_hash.removeprefix("sha256:")
    artifact = artifacts.get(ArtifactRef(uri=f"artifact://{digest}"))
    assert artifact.data
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="alice")
    outcome = service.execute(task_id=task_id, plan_hash=plan_hash)
    assert outcome.task.status is TaskStatus.READY_TO_PUBLISH


def test_redo_still_refused_while_task_in_flight(repository, artifacts, clock, settings):
    service, _ = _flaky_service(repository, artifacts, clock, settings)
    _plan(service)
    with pytest.raises(StateConflictError, match="not COMPLETED or FAILED"):
        _redo(service)


def _ready_to_publish(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, adapter, _ = _service(tmp_path, clock)
    outcome = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main",
        base_sha=BASE_SHA,
    )
    task_id, plan_hash = outcome.task.task_id, outcome.task.plan_hash
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="alice")
    executed = service.execute(task_id=task_id, plan_hash=plan_hash)
    assert executed.task.status is TaskStatus.READY_TO_PUBLISH
    return service, adapter, task_id, plan_hash


def test_reopen_then_reexecute(tmp_path):
    service, adapter, task_id, plan_hash = _ready_to_publish(tmp_path)
    task = service.reopen(
        task_id=task_id, plan_hash=plan_hash, requested_by="bob",
        reason="empty patch",
    )
    assert task.status is TaskStatus.NEEDS_REAPPROVAL
    reasons = [e["reason"] for e in service.status(task_id=task_id).events]
    assert "reopened by bob: empty patch" in reasons

    calls = adapter.execute_calls
    result = service.reexecute(task_id=task_id, plan_hash=plan_hash, requested_by="bob")
    assert adapter.execute_calls == calls + 1
    assert result.task.status is TaskStatus.READY_TO_PUBLISH


def test_reopen_then_replan(tmp_path):
    service, _, task_id, plan_hash = _ready_to_publish(tmp_path)
    service.reopen(task_id=task_id, plan_hash=plan_hash, requested_by="bob", reason="x")
    task = service.replan(task_id=task_id, plan_hash=plan_hash, approver="bob")
    assert task.status is TaskStatus.PLANNING


def test_reopen_is_idempotent(tmp_path):
    service, _, task_id, plan_hash = _ready_to_publish(tmp_path)
    service.reopen(task_id=task_id, plan_hash=plan_hash, requested_by="bob", reason="x")
    events_before = len(service.status(task_id=task_id).events)
    task = service.reopen(task_id=task_id, plan_hash=plan_hash, requested_by="bob", reason="x")
    assert task.status is TaskStatus.NEEDS_REAPPROVAL
    assert len(service.status(task_id=task_id).events) == events_before


def test_reopen_refuses_other_states_and_wrong_hash(tmp_path):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    service, _, _ = _service(tmp_path, clock)
    outcome = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main",
        base_sha=BASE_SHA,
    )
    task_id, plan_hash = outcome.task.task_id, outcome.task.plan_hash
    with pytest.raises(StateConflictError, match="not READY_TO_PUBLISH"):
        service.reopen(task_id=task_id, plan_hash=plan_hash, requested_by="bob", reason="x")
    with pytest.raises(PlanHashMismatchError):
        service.reopen(
            task_id=task_id, plan_hash="sha256:" + "0" * 64,
            requested_by="bob", reason="x",
        )
    assert service.status(task_id=task_id).task.status is TaskStatus.AWAITING_APPROVAL


# --- approve shorthand resolution (no task id / hash from the human) ---


def _plan_issue(service, issue):
    return service.plan(
        repository="owner/repo", issue_number=issue, base_branch="main",
        base_sha=BASE_SHA, issue_title="Build", issue_body="compile.",
    )


def test_resolve_approvable_ignores_other_issues(repository, artifacts, clock, settings):
    # Two issues awaiting approval used to make the global shorthand
    # ambiguous (forcing the explicit hash); per-issue resolution is unique.
    service, _ = _flaky_service(repository, artifacts, clock, settings)
    one = _plan_issue(service, 1)
    two = _plan_issue(service, 2)
    assert service.resolve_approvable(repository="owner/repo", issue_number=1) == (
        one.task.task_id, one.task.plan_hash,
    )
    assert service.resolve_approvable(repository="Owner/Repo", issue_number=2) == (
        two.task.task_id, two.task.plan_hash,
    )


def test_resolve_approvable_accepts_approved_for_resume(repository, artifacts, clock, settings):
    service, _ = _flaky_service(repository, artifacts, clock, settings)
    plan = _plan_issue(service, 1)
    service.approve(task_id=plan.task.task_id, plan_hash=plan.task.plan_hash, approver="alice")
    resolved = service.resolve_approvable(repository="owner/repo", issue_number=1)
    assert resolved == (plan.task.task_id, plan.task.plan_hash)
    # Same approver replays the approval: no error, task still APPROVED
    # (the webhook then resumes the pipeline).
    replay = service.approve(task_id=resolved[0], plan_hash=resolved[1], approver="alice")
    assert replay.duplicate
    assert replay.task.status is TaskStatus.APPROVED


def test_resolve_approvable_refuses_finished_or_missing(repository, artifacts, clock, settings):
    service, adapter = _flaky_service(repository, artifacts, clock, settings)
    with pytest.raises(StateConflictError, match="comment run first"):
        service.resolve_approvable(repository="owner/repo", issue_number=1)
    adapter.empty = True
    with pytest.raises(AdapterContractError):
        _plan_issue(service, 1)
    with pytest.raises(StateConflictError, match="is FAILED, nothing to approve"):
        service.resolve_approvable(repository="owner/repo", issue_number=1)


def test_resolve_approvable_follows_latest_revision(repository, artifacts, clock, settings):
    service, adapter = _flaky_service(repository, artifacts, clock, settings)
    adapter.empty = True
    with pytest.raises(AdapterContractError):
        _plan(service)
    adapter.empty = False
    second = _redo(service)
    assert service.resolve_approvable(repository="owner/repo", issue_number=1) == (
        second.task.task_id, second.task.plan_hash,
    )


# --- plan asks for human input -> answer on the issue -> new revision ---


def _answer(service, answers):
    return service.answer_plan(
        repository="owner/repo", issue_number=1, base_branch="main",
        base_sha=BASE_SHA, issue_title="Build", issue_body="compile.", answers=answers,
    )


def test_input_needed_plan_cannot_be_approved(repository, artifacts, clock, settings):
    service, adapter = _flaky_service(repository, artifacts, clock, settings)
    adapter.ask = True
    asked = _plan(service)
    assert asked.task.status is TaskStatus.AWAITING_APPROVAL
    reasons = [e["reason"] for e in service.status(task_id=asked.task.task_id).events]
    assert reasons[-1] == "plan needs human input"
    with pytest.raises(StateConflictError, match="needs human input"):
        service.approve(
            task_id=asked.task.task_id, plan_hash=asked.task.plan_hash, approver="alice",
        )
    with pytest.raises(StateConflictError, match="agent-platform answer"):
        service.resolve_approvable(repository="owner/repo", issue_number=1)
    assert repository.get_task(asked.task.task_id).status is TaskStatus.AWAITING_APPROVAL


def test_answer_replans_with_questions_and_all_answers(repository, artifacts, clock, settings):
    service, adapter = _flaky_service(repository, artifacts, clock, settings)
    adapter.ask = True
    asked = _plan(service)
    adapter.ask = False
    answered = _answer(service, ["1024 fixed", "  ", "add --max-tokens to override"])

    assert answered.task.revision == 2
    assert answered.task.status is TaskStatus.AWAITING_APPROVAL
    old = repository.get_task(asked.task.task_id)
    assert old.status is TaskStatus.CANCELLED
    old_reasons = [e["reason"] for e in service.status(task_id=old.task_id).events]
    assert old_reasons[-1] == "answered; superseded by revision 2"
    body = adapter.last_job.issue_body
    assert body.startswith("compile.")
    assert "Which max_tokens value should the CLI send?" in body
    assert "### 回答 1\n1024 fixed" in body
    assert "### 回答 2\nadd --max-tokens to override" in body
    # The answered revision is approvable through the shorthand.
    assert service.resolve_approvable(repository="owner/repo", issue_number=1) == (
        answered.task.task_id, answered.task.plan_hash,
    )


def test_answer_refused_when_nothing_was_asked(repository, artifacts, clock, settings):
    service, _ = _flaky_service(repository, artifacts, clock, settings)
    with pytest.raises(StateConflictError, match="comment run first"):
        _answer(service, ["x"])
    normal = _plan(service)
    with pytest.raises(StateConflictError, match="not waiting for an answer"):
        _answer(service, ["x"])
    assert repository.get_task(normal.task.task_id).status is TaskStatus.AWAITING_APPROVAL


def test_empty_answer_refused_without_side_effects(repository, artifacts, clock, settings):
    service, adapter = _flaky_service(repository, artifacts, clock, settings)
    adapter.ask = True
    asked = _plan(service)
    with pytest.raises(StateConflictError, match="answer is empty"):
        _answer(service, ["", "   "])
    assert repository.get_task(asked.task.task_id).status is TaskStatus.AWAITING_APPROVAL
    assert repository.latest_revision("owner/repo", 1) == 1


def test_second_round_of_questions_keeps_answering(repository, artifacts, clock, settings):
    service, adapter = _flaky_service(repository, artifacts, clock, settings)
    adapter.ask = True
    _plan(service)
    again = _answer(service, ["first"])
    assert again.task.revision == 2
    assert again.plan.requires_human_input
    adapter.ask = False
    final = _answer(service, ["first", "second"])
    assert final.task.revision == 3
    assert "### 回答 1\nfirst" in adapter.last_job.issue_body
    assert "### 回答 2\nsecond" in adapter.last_job.issue_body


# --- read-only pre-checks the webhook runs before acknowledging ---


def test_redo_on_a_plan_waiting_for_answers_is_refused_with_its_state(
    repository, artifacts, clock, settings,
):
    """Redo on a plan that asked questions. The
    refusal carries the task state so the issue can be told to answer."""
    service, adapter = _flaky_service(repository, artifacts, clock, settings)
    adapter.ask = True
    asked = _plan(service)
    with pytest.raises(TaskStateRejectedError) as excinfo:
        service.check_redo(repository="owner/repo", issue_number=1)
    assert excinfo.value.task_id == asked.task.task_id
    assert excinfo.value.status == "AWAITING_APPROVAL"
    assert excinfo.value.needs_input is True
    # Read-only: nothing changed, no new revision.
    assert repository.get_task(asked.task.task_id).status is TaskStatus.AWAITING_APPROVAL
    assert repository.latest_revision("owner/repo", 1) == 1
    service.check_answer(repository="owner/repo", issue_number=1)


def test_answer_precheck_refuses_a_plan_that_asked_nothing(
    repository, artifacts, clock, settings,
):
    service, _ = _flaky_service(repository, artifacts, clock, settings)
    normal = _plan(service)
    with pytest.raises(TaskStateRejectedError) as excinfo:
        service.check_answer(repository="owner/repo", issue_number=1)
    assert (excinfo.value.task_id, excinfo.value.status, excinfo.value.needs_input) == (
        normal.task.task_id, "AWAITING_APPROVAL", False,
    )
    with pytest.raises(TaskStateRejectedError) as redo_exc:
        service.check_redo(repository="owner/repo", issue_number=1)
    assert redo_exc.value.needs_input is False
