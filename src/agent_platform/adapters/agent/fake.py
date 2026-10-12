"""Deterministic fake agent adapter for offline tests and local validation."""

from agent_platform.domain.models import (
    ExecuteJobSpec,
    ExecutionResult,
    PlanJobSpec,
    PlanResult,
    ProposedChange,
    ReviewJobSpec,
    ReviewResult,
)


class FakeAgentAdapter:
    """Fully deterministic — same input always yields the same output.

    Never fails, so "RECEIVED -> APPROVED reproducible with the fake
    adapter" does not depend on external systems.
    """

    def plan(self, job: PlanJobSpec) -> PlanResult:
        return PlanResult(
            task_id=job.task_id,
            repository=job.repository,
            base_sha=job.base_sha,
            summary=f"[fake] plan for {job.repository}#{job.issue_number}",
            proposed_changes=[
                ProposedChange(
                    path="docs/issues/issue-0001.md",
                    reason="deterministic fake change",
                )
            ],
            test_commands=["pytest -q"],
        )

    def execute(self, job: ExecuteJobSpec) -> ExecutionResult:
        return ExecutionResult(
            task_id=job.task_id,
            plan_hash=job.plan_hash,
            base_sha=job.base_sha,
            status="succeeded",
            changed_files=list(job.allowed_paths),
            pr_body_draft=f"[fake] implements plan {job.plan_hash}",
        )

    def review(self, job: ReviewJobSpec) -> ReviewResult:
        return ReviewResult(
            task_id=job.task_id,
            repository=job.repository,
            plan_hash=job.plan_hash,
            base_sha=job.base_sha,
            head_sha=job.head_sha,
            summary="[fake] no findings",
        )
