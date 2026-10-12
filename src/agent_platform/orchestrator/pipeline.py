"""Post-approval pipeline (README「処理の流れ」): once a human approves a
plan, execute -> publish (Draft PR) -> review run without further human
commands.

The approval gate is unchanged: every step goes through TaskService, whose
entry points re-check the active approval (require_active_approval). The
pipeline only removes the manual `agentctl execute/publish/review` typing
between the approval and the Draft PR.

A step that ends in a non-continuing state (NEEDS_REAPPROVAL, FAILED, ...)
stops the pipeline; the task stays in that state for the existing
reopen / reexecute / replan / redo paths. Failures are never swallowed:
they are reported (log + notify) and the task state records them.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from agent_platform.domain.models import TaskStatus
from agent_platform.redaction import redact_text


class PipelineSteps(Protocol):
    """The three stages, each returning the task status it ended in.
    Implemented by the CLI wiring (k8s adapters + GitHub publisher)."""

    def execute(self, task_id: str, plan_hash: str) -> TaskStatus: ...

    def publish(self, task_id: str, plan_hash: str) -> tuple[TaskStatus, int | None]: ...

    def review(self, task_id: str, plan_hash: str, pr_number: int) -> TaskStatus: ...

    def notify_stopped(self, task_id: str, stage: str, status: str) -> None: ...


@dataclass
class PipelineReport:
    task_id: str
    stages: list[tuple[str, str]] = field(default_factory=list)
    stopped_at: str | None = None
    error: str | None = None
    pr_number: int | None = None


def run_pipeline(
    steps: PipelineSteps, task_id: str, plan_hash: str, *, review_enabled: bool
) -> PipelineReport:
    """Run execute -> publish -> review for one approved task."""
    report = PipelineReport(task_id=task_id)

    def _stop(stage: str, status: str, error: str | None = None) -> PipelineReport:
        report.stopped_at = stage
        report.error = error
        steps.notify_stopped(task_id, stage, status)
        return report

    stage = "execute"
    try:
        status = steps.execute(task_id, plan_hash)
        report.stages.append((stage, status.value))
        if status is not TaskStatus.READY_TO_PUBLISH:
            return _stop(stage, status.value)

        stage = "publish"
        status, pr_number = steps.publish(task_id, plan_hash)
        report.stages.append((stage, status.value))
        report.pr_number = pr_number
        if status is not TaskStatus.PR_CREATED or pr_number is None:
            return _stop(stage, status.value)

        if not review_enabled:
            return report
        stage = "review"
        status = steps.review(task_id, plan_hash, pr_number)
        report.stages.append((stage, status.value))
        if status not in (TaskStatus.COMPLETED, TaskStatus.REVIEWING):
            return _stop(stage, status.value)
        return report
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        return _stop(stage, "error", redact_text(f"{type(exc).__name__}: {exc}")[:300])


class PipelineRunner:
    """Single background worker: approvals are queued and run one at a time
    (the webhook must answer GitHub within seconds; execute takes minutes).
    Serial execution also keeps the SQLite state and the review context
    single-writer.

    Queued work is in memory only: a task approved right before a pod
    restart stays APPROVED and can be resumed with `agentctl execute`.
    """

    def __init__(
        self,
        steps: PipelineSteps,
        *,
        review_enabled: bool,
        log: Callable[[str], None],
    ) -> None:
        self._steps = steps
        self._review_enabled = review_enabled
        self._log = log
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="pipeline", daemon=True)
        self._thread.start()

    def submit(self, task_id: str, plan_hash: str) -> None:
        self._log(f"pipeline: queued {task_id}")
        self._queue.put((task_id, plan_hash))

    def close(self, timeout: float | None = None) -> None:
        """Finish queued work and stop the worker (tests, shutdown)."""
        self._queue.put(None)
        self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            task_id, plan_hash = item
            self._log(f"pipeline: start {task_id}")
            report = run_pipeline(
                self._steps, task_id, plan_hash, review_enabled=self._review_enabled
            )
            stages = " ".join(f"{s}={st}" for s, st in report.stages) or "(none)"
            if report.stopped_at is None:
                self._log(f"pipeline: done {task_id} {stages} pr=#{report.pr_number}")
            else:
                detail = f" error={report.error}" if report.error else ""
                self._log(
                    f"pipeline: stopped {task_id} at {report.stopped_at} {stages}{detail}"
                )
