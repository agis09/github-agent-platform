"""RemotePlanAdapter: runs plan() as a one-shot Kubernetes Job.

Implements the AgentAdapter protocol by delegating to a JobLauncher —
TaskService stays K8s-unaware (see docs/design.md). The worker inside
the job runs the agent runtime; this adapter only handles launch,
wait, and result collection.

Failure mapping (no ambiguous success):
  - launch conflict (active job with same task) -> AdapterError
  - wait timeout                                 -> AdapterTimeoutError
  - job failed                                   -> AdapterError (reason +
    worker log tail)
  - missing result markers                       -> AdapterError
"""

import json

from agent_platform.adapters.kubernetes.base import JobLauncher
from agent_platform.adapters.kubernetes.models import (
    JobHandle,
    JobLaunchSpec,
    JobPhase,
)
from agent_platform.domain.models import (
    ExecuteJobSpec,
    ExecutionResult,
    PlanJobSpec,
    PlanResult,
    ReviewJobSpec,
    ReviewResult,
)
from agent_platform.errors import AdapterContractError, AdapterError


class RemotePlanAdapter:
    """AgentAdapter for plan() backed by a K8s JobLauncher.

    This adapter handles plan() only; execute/review have their own adapters.
    """

    def __init__(
        self,
        launcher: JobLauncher,
        *,
        plan_image: str,
        deadline_seconds: int = 900,
        ttl_seconds: int = 600,
    ) -> None:
        self._launcher = launcher
        self._plan_image = plan_image
        self._deadline_seconds = deadline_seconds
        self._ttl_seconds = ttl_seconds

    def plan(self, job: PlanJobSpec) -> PlanResult:
        issue_title = job.issue_title or ""
        issue_body = job.issue_body or ""
        if not issue_title and not issue_body:
            msg = (
                "remote plan requires issue content (issue_title/issue_body); "
                "the worker cannot fetch GitHub itself"
            )
            raise AdapterError(msg)
        spec = JobLaunchSpec(
            task_id=job.task_id,
            repository=job.repository,
            issue_number=job.issue_number,
            base_branch=job.base_branch,
            base_sha=job.base_sha,
            issue_title=issue_title,
            issue_body=issue_body,
            plan_image=self._plan_image,
            deadline_seconds=self._deadline_seconds,
            ttl_seconds=self._ttl_seconds,
        )
        handle: JobHandle = self._launcher.launch(spec)
        status = self._launcher.wait(
            handle, timeout_seconds=float(self._deadline_seconds)
        )
        if status.phase == JobPhase.FAILED:
            detail = f"reason={status.reason}"
            if status.log_tail:
                detail += f" worker: {status.log_tail.strip()[-300:]}"
            msg = f"plan job {handle.job_name} failed: {detail}"
            raise AdapterError(msg)
        raw = self._launcher.collect(handle)
        return self._parse(raw, job)

    def _parse(self, raw: bytes, job: PlanJobSpec) -> PlanResult:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            msg = f"plan job returned invalid JSON: {exc}"
            raise AdapterContractError(msg) from exc
        if not isinstance(payload, dict):
            msg = "plan job result is not a JSON object"
            raise AdapterContractError(msg)
        # The orchestrator recomputes plan_hash; the worker never supplies one.
        payload.pop("plan_hash", None)
        try:
            plan = PlanResult.model_validate(payload)
        except ValueError as exc:
            msg = f"plan job result violates PlanResult contract: {exc}"
            raise AdapterContractError(msg) from exc
        if plan.task_id != job.task_id or plan.repository != job.repository:
            msg = (
                f"plan job echoed wrong task identity: {plan.task_id!r}/"
                f"{plan.repository!r}"
            )
            raise AdapterContractError(msg)
        if plan.base_sha != job.base_sha:
            msg = (
                f"plan job echoed wrong base_sha: {plan.base_sha!r} != "
                f"{job.base_sha!r}"
            )
            raise AdapterContractError(msg)
        return plan

    def execute(self, job: ExecuteJobSpec) -> ExecutionResult:
        msg = "RemotePlanAdapter handles plan() only; use RemoteExecuteAdapter for execute()"
        raise NotImplementedError(msg)

    def review(self, job: ReviewJobSpec) -> ReviewResult:
        msg = "RemotePlanAdapter handles plan() only; use RemoteReviewAdapter for review()"
        raise NotImplementedError(msg)
