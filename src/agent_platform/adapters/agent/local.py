"""LocalAgentAdapter: runs plan() with the configured agent CLI on this host.

Used by `AGENT_PLATFORM_ADAPTER=local` (developer runs) and by the plan
worker inside the Kubernetes Plan Job. The agent CLI (codex, claude, ...)
is chosen by AGENT_PLATFORM_AGENT_RUNTIME; see agent_platform.agent_runtime.

execute()/review() are not offered locally: they run only inside the
Kubernetes sandbox (worker.execute / worker.review), where the Pod
boundary isolates the writing agent.
"""

import json
from pathlib import Path

from agent_platform.agent_runtime import AgentRuntime, RunMode, RunOutcome, build_runtime
from agent_platform.config import Settings
from agent_platform.domain.models import (
    ExecuteJobSpec,
    ExecutionResult,
    PlanJobSpec,
    PlanResult,
    ReviewJobSpec,
    ReviewResult,
)
from agent_platform.errors import AdapterContractError, AdapterError
from agent_platform.prompt_builder import build_plan_prompt
from agent_platform.schema_export import schema_file


class LocalAgentAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        runtime: AgentRuntime | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self._runtime = runtime or build_runtime(settings)
        self._repo_root = repo_root
        self._last_outcome: RunOutcome | None = None

    @property
    def last_outcome(self) -> RunOutcome | None:
        """The most recent agent run (audit input for the plan worker —
        only RunOutcome.audit_line() is logged, never the message text)."""
        return self._last_outcome

    def plan(self, job: PlanJobSpec) -> PlanResult:
        try:
            schema = schema_file("plan_result")
        except FileNotFoundError as exc:
            raise AdapterError(str(exc)) from exc
        outcome = self._runtime.run(
            prompt=build_plan_prompt(job, repo_root=self._repo_root),
            mode=RunMode.READ_ONLY,
            cwd=self._repo_root,
            output_schema=schema,
        )
        self._last_outcome = outcome
        if outcome.returncode != 0:
            msg = (
                f"{outcome.runtime} exited with code {outcome.returncode}: "
                f"{outcome.failure_detail()}"
            )
            raise AdapterError(msg)
        if outcome.error is not None:
            msg = f"{outcome.runtime} run failed: {outcome.error}"
            raise AdapterError(msg)
        return parse_plan_result(outcome)

    def execute(self, job: ExecuteJobSpec) -> ExecutionResult:
        msg = "execute runs only in the Kubernetes sandbox (AGENT_PLATFORM_ADAPTER=k8s)"
        raise NotImplementedError(msg)

    def review(self, job: ReviewJobSpec) -> ReviewResult:
        msg = "review runs only in the Kubernetes sandbox (AGENT_PLATFORM_ADAPTER=k8s)"
        raise NotImplementedError(msg)


def parse_plan_result(outcome: RunOutcome) -> PlanResult:
    last = outcome.final_message
    if last is None:
        msg = f"{outcome.runtime} produced no final message"
        raise AdapterError(msg)
    try:
        data = json.loads(last)
    except json.JSONDecodeError as exc:
        msg = f"final agent message is not JSON: {exc}"
        raise AdapterContractError(msg) from exc
    if not isinstance(data, dict):
        msg = f"final agent message must be a JSON object, got {type(data).__name__}"
        raise AdapterContractError(msg)
    # plan_hash is orchestrator-computed; an adapter-supplied hash is never
    # trusted — force empty before validation.
    data.pop("plan_hash", None)
    try:
        return PlanResult.model_validate(data)
    except Exception as exc:  # pydantic ValidationError surfaced as contract error
        msg = f"agent output violates plan_result contract: {exc}"
        raise AdapterContractError(msg) from exc
