"""Contract: agent adapters satisfy the same behavioral contract.

Adding an adapter to ADAPTERS applies the same tests to it.
"""

from typing import Final

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.domain.models import (
    ExecuteJobSpec,
    PlanJobSpec,
    ReviewJobSpec,
)

ADAPTERS: Final = [("fake", FakeAgentAdapter)]

BASE_SHA: Final = "a" * 40
HEAD_SHA: Final = "b" * 40
PLAN_HASH: Final = "sha256:" + "c" * 64


def _specs():
    return [
        PlanJobSpec(
            task_id="task-contract01",
            repository="owner/repo",
            issue_number=1,
            base_branch="main",
            base_sha=BASE_SHA,
        ),
        ExecuteJobSpec(
            task_id="task-contract01",
            repository="owner/repo",
            plan_hash=PLAN_HASH,
            base_branch="main",
            base_sha=BASE_SHA,
            allowed_paths=["docs/x.md"],
            test_commands=["pytest -q"],
            plan_ref=_plan_ref(),
        ),
        ReviewJobSpec(
            task_id="task-contract01",
            repository="owner/repo",
            plan_hash=PLAN_HASH,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        ),
    ]


def _plan_ref():
    return f"artifact://{'c' * 64}"


@pytest.mark.parametrize(("name", "adapter_factory"), ADAPTERS)
def test_adapter_implements_protocol(name, adapter_factory):
    adapter = adapter_factory()
    for method in ("plan", "execute", "review"):
        assert callable(getattr(adapter, method))


@pytest.mark.parametrize(("name", "adapter_factory"), ADAPTERS)
def test_adapter_deterministic(name, adapter_factory):
    adapter = adapter_factory()
    jobs = _specs()
    for job in jobs:
        method = getattr(adapter, type(job).__name__.removesuffix("JobSpec").lower())
        first = method(job)
        second = method(job)
        assert first == second


@pytest.mark.parametrize(("name", "adapter_factory"), ADAPTERS)
def test_adapter_echoes_task_identity(name, adapter_factory):
    adapter = adapter_factory()
    jobs = _specs()
    plan = adapter.plan(jobs[0])
    assert plan.task_id == "task-contract01"
    assert plan.repository == "owner/repo"
    assert plan.base_sha == BASE_SHA
    execution = adapter.execute(jobs[1])
    assert execution.task_id == "task-contract01"
    assert execution.plan_hash == PLAN_HASH
    assert execution.base_sha == BASE_SHA
    review = adapter.review(jobs[2])
    assert review.task_id == "task-contract01"
    assert review.head_sha == HEAD_SHA


@pytest.mark.parametrize(("name", "adapter_factory"), ADAPTERS)
def test_adapter_results_validate(name, adapter_factory):
    adapter = adapter_factory()
    jobs = _specs()
    plan = adapter.plan(jobs[0])
    assert plan.plan_hash == ""  # orchestrator recomputes; adapter never sets it
