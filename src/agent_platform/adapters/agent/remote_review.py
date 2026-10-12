"""RemoteReviewAdapter: runs review() as a one-shot READ-ONLY Job.

The reviewer is a separate codex context from the implementing agent.
The Job template has no workspace volume and no credentials — the review
agent cannot modify code or push.
"""

import json
from collections.abc import Callable
from typing import Any, cast

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

_REVIEW_BEGIN = "REVIEW_RESULT_BEGIN"
_REVIEW_END = "REVIEW_RESULT_END"


class RemoteReviewAdapter:
    """AgentAdapter for review() backed by a read-only K8s Job."""

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
        self._last_review: ReviewResult | None = None
        self._payload_builder: Callable[[ReviewJobSpec], dict[str, str]] | None = (
            None
        )

    @property
    def last_review(self) -> ReviewResult | None:
        return self._last_review

    def plan(self, job: PlanJobSpec) -> PlanResult:
        msg = "RemoteReviewAdapter.plan() is handled by RemotePlanAdapter"
        raise NotImplementedError(msg)

    def execute(self, job: ExecuteJobSpec) -> ExecutionResult:
        msg = "RemoteReviewAdapter.execute() is handled by RemoteExecuteAdapter"
        raise NotImplementedError(msg)

    def review(self, job: ReviewJobSpec) -> ReviewResult:
        spec = JobLaunchSpec(
            task_id=job.task_id,
            repository=job.repository,
            issue_number=1,
            base_branch="main",
            base_sha=job.base_sha,
            issue_title=f"review:{job.plan_hash}",
            issue_body=f"diff_review_for_{job.head_sha}",
            plan_image=self._plan_image,
            kind=JobKind.REVIEW,
            plan_hash=job.plan_hash,
            deadline_seconds=self._deadline_seconds,
            ttl_seconds=self._ttl_seconds,
        )
        # The diff/plan/issue/tests payload travels through the launcher's
        # review extras (a dict of template values set just before render).
        if self._payload_builder is not None:
            # Same wiring package: the launcher's renderer reads this attr.
            launcher = cast("Any", self._launcher)
            launcher._review_extras = self._payload_builder(job)
        handle: JobHandle = self._launcher.launch(spec)
        status = self._launcher.wait(
            handle, timeout_seconds=float(self._deadline_seconds)
        )
        if status.phase == JobPhase.FAILED:
            detail = f"reason={status.reason}"
            if status.log_tail:
                detail += f" worker: {status.log_tail.strip()[-300:]}"
            msg = f"review job {handle.job_name} failed: {detail}"
            raise AdapterError(msg)
        raw = self._launcher.collect(handle)
        result = self._parse(raw, job)
        self._last_review = result
        return result


    def with_payload_builder(
        self, builder: "Callable[[ReviewJobSpec], dict[str, str]]"
    ) -> "RemoteReviewAdapter":
        self._payload_builder = builder
        return self

    def _parse(self, raw: bytes, job: ReviewJobSpec) -> ReviewResult:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            msg = f"review job returned invalid JSON: {exc}"
            raise AdapterContractError(msg) from exc
        if not isinstance(payload, dict):
            msg = "review job result is not a JSON object"
            raise AdapterContractError(msg)
        try:
            result = ReviewResult.model_validate(payload)
        except ValueError as exc:
            msg = f"review job result violates ReviewResult contract: {exc}"
            raise AdapterContractError(msg) from exc
        if result.task_id != job.task_id or result.repository != job.repository:
            msg = f"review job echoed wrong identity: {result.task_id!r}"
            raise AdapterContractError(msg)
        return result
