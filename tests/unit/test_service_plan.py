"""TaskService.plan: full RECEIVED->AWAITING_APPROVAL flow, idempotency,
adapter contract enforcement."""

import pytest

from agent_platform.domain.hashing import compute_plan_hash
from agent_platform.domain.models import PlanJobSpec, TaskStatus
from agent_platform.errors import AdapterContractError
from tests.conftest import BASE_SHA


def test_plan_reaches_awaiting_approval(service):
    outcome = service.plan(

        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA

    )
    assert outcome.task.status == TaskStatus.AWAITING_APPROVAL
    assert not outcome.duplicate


def test_plan_twice_same_task_no_duplicate(recording_service):
    service, adapter = recording_service
    first = service.plan(

        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA

    )
    second = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA
    )
    assert first.task.task_id == second.task.task_id
    assert second.duplicate is True
    assert adapter.plan_calls == 1  # second run did not re-invoke the adapter


def test_plan_hash_is_orchestrator_computed(service):
    outcome = service.plan(

        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA

    )
    assert outcome.plan.plan_hash == ""  # adapter returned empty
    assert outcome.task.plan_hash == compute_plan_hash(outcome.plan)


def test_plan_artifact_digest_equals_plan_hash_hex(service):
    outcome = service.plan(

        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA

    )
    digest = outcome.task.plan_hash.removeprefix("sha256:")
    stored = service._artifacts.get(_ref(digest))
    assert stored.data


def test_plan_different_issue_creates_distinct_task(service):
    a = service.plan(repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA)
    b = service.plan(repository="owner/repo", issue_number=2, base_branch="main", base_sha=BASE_SHA)
    assert a.task.task_id != b.task.task_id


def test_plan_adapter_identity_violation_fails_task(repository, artifacts, clock, settings):
    from agent_platform.adapters.agent.fake import FakeAgentAdapter

    class WrongEchoAdapter(FakeAgentAdapter):
        def plan(self, job: PlanJobSpec):
            result = super().plan(job)
            return result.model_copy(update={"task_id": "task-XXXXXXXXXXXX"})

    service = _make_service(repository, artifacts, clock, settings, WrongEchoAdapter())
    with pytest.raises(AdapterContractError):
        service.plan(repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA)
    assert repository.get_task(_expected_task_id()).status == TaskStatus.FAILED


def test_plan_repo_case_insensitive_same_task(service):
    a = service.plan(repository="Owner/Repo", issue_number=1, base_branch="main", base_sha=BASE_SHA)
    b = service.plan(repository="owner/REPO", issue_number=1, base_branch="main", base_sha=BASE_SHA)
    assert a.task.task_id == b.task.task_id
    assert b.duplicate is True


def test_plan_without_proposed_changes_fails_task(repository, artifacts, clock, settings):
    """A plan that names no files (and does not require human input) can
    never publish — approve() would derive an empty allowlist and every
    execution would violate scope. The gate fires at plan time."""
    from agent_platform.adapters.agent.fake import FakeAgentAdapter

    class EmptyPlanAdapter(FakeAgentAdapter):
        def plan(self, job: PlanJobSpec):
            result = super().plan(job)
            return result.model_copy(update={"proposed_changes": []})

    service = _make_service(repository, artifacts, clock, settings, EmptyPlanAdapter())
    with pytest.raises(AdapterContractError, match="proposed no file changes"):
        service.plan(repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA)
    assert repository.get_task(_expected_task_id()).status == TaskStatus.FAILED


def test_plan_human_input_only_allowed_without_changes(repository, artifacts, clock, settings):
    """requires_human_input=true with no file changes is a valid
    questions-only plan — the gate must not fire."""
    from agent_platform.adapters.agent.fake import FakeAgentAdapter

    class QuestionsOnlyAdapter(FakeAgentAdapter):
        def plan(self, job: PlanJobSpec):
            result = super().plan(job)
            return result.model_copy(
                update={"proposed_changes": [], "requires_human_input": True}
            )

    service = _make_service(repository, artifacts, clock, settings, QuestionsOnlyAdapter())
    outcome = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA
    )
    assert outcome.task.status == TaskStatus.AWAITING_APPROVAL


def _expected_task_id():
    from agent_platform.domain.models import derive_task_id

    return derive_task_id("owner/repo", 1)


def _expected_task_id_alias():  # pragma: no cover - readability helper
    return _expected_task_id()


def _ref(digest: str):
    from agent_platform.domain.models import ArtifactRef

    return ArtifactRef(uri=f"artifact://{digest}")


def _make_service(repository, artifacts, clock, settings, adapter):
    from agent_platform.orchestrator.service import TaskService

    return TaskService(
        repository=repository,
        adapter=adapter,
        artifacts=artifacts,
        clock=clock,
        settings=settings,
    )
