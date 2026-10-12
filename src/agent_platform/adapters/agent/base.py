"""AgentAdapter protocol.

Extension point for swapping agent engines (fake, remote Kubernetes Jobs, ...).
Adapters must return results matching the domain models; the orchestrator
recomputes plan_hash and never trusts an adapter-supplied hash.
"""

from typing import Protocol

from agent_platform.domain.models import (
    ExecuteJobSpec,
    ExecutionResult,
    PlanJobSpec,
    PlanResult,
    ReviewJobSpec,
    ReviewResult,
)


class AgentAdapter(Protocol):
    def plan(self, job: PlanJobSpec) -> PlanResult: ...

    def execute(self, job: ExecuteJobSpec) -> ExecutionResult: ...

    def review(self, job: ReviewJobSpec) -> ReviewResult: ...
