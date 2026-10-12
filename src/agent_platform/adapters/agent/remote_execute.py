"""RemoteExecuteAdapter: runs execute() as a one-shot Kubernetes Job.

Mirrors RemotePlanAdapter but for ExecuteJobSpec. The worker inside the job
runs the codex agent with a workspace-write sandbox over a repository
snapshot pinned at the approval's base SHA; this adapter handles launch,
wait, result collection, and contract validation.

Failure mapping (no ambiguous success):
  - launch conflict            -> AdapterError
  - wait timeout               -> AdapterTimeoutError
  - job failed (K8s reason)    -> AdapterError (reason + worker log tail)
  - missing result markers     -> AdapterError
  - contract violation         -> AdapterContractError
"""

import json
from collections.abc import Callable

from agent_platform.adapters.kubernetes.base import JobLauncher
from agent_platform.adapters.kubernetes.models import (
    JobHandle,
    JobKind,
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

_LOG_MARKER_BEGIN = "EXECUTION_RESULT_BEGIN"
_LOG_MARKER_END = "EXECUTION_RESULT_END"


_PATCH_SEPARATOR = "\n---PATCH---\n"


def _split_patch(raw: bytes) -> tuple[bytes, str | None]:
    if _PATCH_SEPARATOR.encode() in raw:
        head, _, patch = raw.partition(_PATCH_SEPARATOR.encode())
        return head, patch.decode("utf-8")
    return raw, None


class RemoteExecuteAdapter:
    """AgentAdapter for execute() backed by a K8s JobLauncher.

    plan/review stay delegated: plan goes to the wrapped plan adapter, review
    has its own adapter (RemoteReviewAdapter).
    """

    def __init__(
        self,
        launcher: JobLauncher,
        *,
        plan_image: str,
        deadline_seconds: int = 1800,
        ttl_seconds: int = 600,
        snapshot_configmap: str = "",
    ) -> None:
        self._launcher = launcher
        self._plan_image = plan_image
        self._deadline_seconds = deadline_seconds
        self._ttl_seconds = ttl_seconds
        self._snapshot_configmap = snapshot_configmap
        self._last_patch: str | None = None

    # The orchestrator calls execute() only after the approval gate passed;
    # plan() is handled by RemotePlanAdapter in the k8s wiring.
    def plan(self, job: PlanJobSpec) -> PlanResult:
        msg = "RemoteExecuteAdapter.plan() is handled by RemotePlanAdapter"
        raise NotImplementedError(msg)

    @property
    def last_patch(self) -> str | None:
        """The patch from the most recent execute() — the CLI persists it to
        a file for agentctl publish."""
        return self._last_patch

    def execute(self, job: ExecuteJobSpec) -> ExecutionResult:
        spec = JobLaunchSpec(
            task_id=job.task_id,
            repository=job.repository,
            issue_number=1,  # unused by the execute entrypoint
            base_branch=job.base_branch,
            base_sha=job.base_sha,
            issue_title=f"execute:{job.plan_hash}",
            issue_body=f"allowed={','.join(job.allowed_paths)}; "
                       f"tests={';'.join(job.test_commands)}",
            plan_image=self._plan_image,
            kind=JobKind.EXECUTE,
            snapshot_configmap=self._snapshot_configmap,
            prompt=self._prompt_builder(job),
            plan_hash=job.plan_hash,
            allowed_paths=list(job.allowed_paths),
            test_commands=list(job.test_commands),
            search_forbidden_tokens=list(job.search_forbidden_tokens),
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
            msg = f"execute job {handle.job_name} failed: {detail}"
            raise AdapterError(msg)
        raw = self._launcher.collect(handle)
        result_json, patch = _split_patch(raw)
        self._last_patch = patch
        return self._parse(result_json, job)

    def _prompt_builder(self, job: ExecuteJobSpec) -> str:
        builder = getattr(self, "_prompt", None)
        if builder is None:
            return ""
        return str(builder(job))

    def with_prompt_builder(
        self, builder: "Callable[[ExecuteJobSpec], str]"
    ) -> "RemoteExecuteAdapter":
        """Set a prompt builder: ExecuteJobSpec -> str. Called by the wiring
        so the orchestrator's plan content can reach the worker."""
        self._prompt = builder
        return self

    def _parse(self, raw: bytes, job: ExecuteJobSpec) -> ExecutionResult:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            msg = f"execute job returned invalid JSON: {exc}"
            raise AdapterContractError(msg) from exc
        if not isinstance(payload, dict):
            msg = "execute job result is not a JSON object"
            raise AdapterContractError(msg)
        try:
            result = ExecutionResult.model_validate(payload)
        except ValueError as exc:
            msg = f"execute job result violates ExecutionResult contract: {exc}"
            raise AdapterContractError(msg) from exc
        if result.task_id != job.task_id or result.plan_hash != job.plan_hash:
            msg = (
                f"execute job echoed wrong identity: {result.task_id!r}/"
                f"{result.plan_hash!r}"
            )
            raise AdapterContractError(msg)
        if result.base_sha != job.base_sha:
            msg = f"execute job echoed wrong base_sha: {result.base_sha!r}"
            raise AdapterContractError(msg)
        return result

    def review(self, job: ReviewJobSpec) -> ReviewResult:
        msg = "RemoteExecuteAdapter does not handle review(); use RemoteReviewAdapter"
        raise NotImplementedError(msg)
