"""TaskService: plan / approve / status use cases.

Rules enforced here (and in the repository backstops):
- plan on the same (repo, issue) never creates a second task
- approve verifies plan_hash, state, and stored-plan consistency
- plan_hash is always recomputed by the orchestrator, never trusted from
  the adapter
- failures leave explicit, retryable state — no silent fallbacks
"""

import datetime
import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_platform.adapters.agent.base import AgentAdapter
from agent_platform.adapters.artifacts.base import ArtifactStore
from agent_platform.clock import Clock
from agent_platform.config import Settings
from agent_platform.contracts import ContractArtifact
from agent_platform.domain.hashing import compute_plan_hash
from agent_platform.domain.models import (
    Approval,
    Artifact,
    ArtifactRef,
    ExecuteJobSpec,
    ExecutionResult,
    PlanJobSpec,
    PlanResult,
    PublishRequest,
    ReviewJobSpec,
    ReviewResult,
    Task,
    TaskStatus,
    approval_is_expired,
    derive_approval_id,
    derive_task_id,
)
from agent_platform.errors import (
    AdapterContractError,
    ApprovalConflictError,
    DependencyNotSatisfiedError,
    ExpiredApprovalError,
    IntegrityError,
    NotFoundError,
    PlanHashMismatchError,
    PlanMismatchError,
    StateConflictError,
    TaskStateRejectedError,
)
from agent_platform.orchestrator.repository import SqliteTaskRepository
from agent_platform.publisher.publisher import PullRequestPublisher

_AUDIT_RETENTION = "audit"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanOutcome:
    task: Task
    plan: PlanResult | None
    duplicate: bool


@dataclass(frozen=True)
class ApprovalOutcome:
    task: Task
    approval: Approval
    duplicate: bool


@dataclass(frozen=True)
class RejectionOutcome:
    task: Task
    duplicate: bool


@dataclass(frozen=True)
class TaskStatusView:
    task: Task
    approval: Approval | None
    approval_expired: bool
    plan: PlanResult | None
    execution: ExecutionResult | None
    events: list[dict[str, str]]


@dataclass(frozen=True)
class ExecutionOutcome:
    task: Task
    execution: ExecutionResult | None
    duplicate: bool


@dataclass(frozen=True)
class ReviewOutcome:
    task: Task
    review: ReviewResult | None
    duplicate: bool


@dataclass(frozen=True)
class DagEdgeOutcome:
    parent_task_id: str
    child_task_id: str
    duplicate: bool


@dataclass(frozen=True)
class ParentStatusView:
    parent_task_id: str
    children: list[dict[str, str]]  # {task_id, repository, status, ...}
    projected_status: str  # 受付/実行中/失敗/完了 as computed
    all_children_complete: bool
    # Rollback material: children whose failure blocks others, plus any
    # contract pins held by failed children (published contracts at risk).
    blocked_followers: dict[str, list[str]] = field(default_factory=dict)
    failed_with_pins: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ContractPublishOutcome:
    name: str
    version: str
    digest: str
    duplicate: bool


@dataclass(frozen=True)
class ContractPinOutcome:
    contract_name: str
    version: str
    digest: str
    consumer_task_id: str


@dataclass(frozen=True)
class PublishOutcome:
    task: Task
    duplicate: bool
    pr_url: str | None
    pr_number: int | None = None


class TaskService:
    def __init__(
        self,
        *,
        repository: SqliteTaskRepository,
        adapter: AgentAdapter,
        artifacts: ArtifactStore,
        clock: Clock,
        settings: Settings,
        publisher: "PullRequestPublisher | None" = None,
        head_sha_provider: "Callable[[str], str] | None" = None,
        repo_root: "Path | None" = None,
    ) -> None:
        self._repo = repository
        self._adapter = adapter
        self._artifacts = artifacts
        self._clock = clock
        self._settings = settings
        self._publisher = publisher
        self._head_sha_provider = head_sha_provider
        # Repository snapshot root for forbidden-vocabulary extraction
        # (offline tests pass None; the CLI wires the sandbox clone when
        # present). Optional — issue-derived tokens still apply without it.
        self._repo_root = repo_root

    # --- helpers ---

    def _now(self) -> str:
        return self._clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _store_plan_artifact(self, plan: PlanResult, plan_hash: str) -> ArtifactRef:
        canonical = json.dumps(
            plan.model_dump(mode="json", exclude={"plan_hash"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        ref = self._artifacts.put(_bytes_artifact(canonical))
        if ref.digest != plan_hash.removeprefix("sha256:"):
            msg = (
                f"plan artifact digest {ref.digest} does not match plan_hash {plan_hash}"
            )
            raise IntegrityError(msg)
        self._repo.insert_artifact_metadata(
            ref.digest, len(canonical), _AUDIT_RETENTION, self._now()
        )
        return ref

    def _load_plan(self, task: Task) -> PlanResult:
        if task.plan_hash is None:
            msg = f"task {task.task_id} has no plan_hash recorded"
            raise StateConflictError(msg)
        ref = ArtifactRef(uri=f"artifact://{task.plan_hash.removeprefix('sha256:')}")
        artifact = self._artifacts.get(ref)
        plan = PlanResult.model_validate_json(artifact.data)
        # Stored plan must agree with the task record; a mismatch is
        # corruption, surfaced loudly (no repair, no fallback).
        if plan.task_id != task.task_id or plan.repository != task.repository:
            msg = f"stored plan disagrees with task record for {task.task_id}"
            raise PlanMismatchError(msg)
        if plan.base_sha != task.base_sha:
            msg = f"stored plan base_sha disagrees with task record for {task.task_id}"
            raise PlanMismatchError(msg)
        if not _verify_hash_bytes(plan, task.plan_hash):
            msg = f"stored plan hash mismatch for {task.task_id}"
            raise PlanMismatchError(msg)
        return plan

    # --- plan ---

    def plan(
        self,
        *,
        repository: str,
        issue_number: int,
        base_branch: str,
        base_sha: str,
        issue_title: str | None = None,
        issue_body: str | None = None,
    ) -> PlanOutcome:
        repository = repository.lower()
        task_id = derive_task_id(repository, issue_number)
        now = self._now()

        existing = self._repo.get_task_by_issue(repository, issue_number)
        if existing is None:
            task = Task(
                task_id=task_id,
                repository=repository,
                issue_number=issue_number,
                created_at=now,
                updated_at=now,
            )
            try:
                self._repo.insert_task(task)
            except sqlite3.IntegrityError as exc:
                # Concurrent same-issue plan; reconcile to the existing row.
                existing = self._repo.get_task_by_issue(repository, issue_number)
                if existing is None:
                    msg = "task vanished during concurrent plan"
                    raise IntegrityError(msg) from exc
        else:
            task = existing

        if task.status not in (TaskStatus.RECEIVED, TaskStatus.PLANNING):
            # Idempotent replay: same command on an existing task returns the
            # existing task without creating anything new.
            plan = self._load_plan(task) if task.plan_hash is not None else None
            return PlanOutcome(task=self._repo.get_task(task.task_id), plan=plan, duplicate=True)

        self._repo.transition_task(
            task.task_id, TaskStatus.PLANNING.value, reason="plan requested", now=now
        )
        job = PlanJobSpec(
            task_id=task.task_id,
            repository=task.repository,
            issue_number=task.issue_number,
            base_branch=base_branch,
            base_sha=base_sha,
            issue_title=issue_title,
            issue_body=issue_body,
        )
        result = self._adapter.plan(job)
        if result.task_id != task.task_id or result.repository != task.repository:
            msg = (
                f"adapter echoed wrong task identity: {result.task_id!r}"
                f" != {task.task_id!r}"
            )
            self._repo.transition_task(
                task.task_id,
                TaskStatus.FAILED.value,
                reason="adapter contract violation",
                now=now,
            )
            raise AdapterContractError(msg)
        plan_hash = compute_plan_hash(result)
        # Derive the search-forbidden vocabulary from the untrusted task
        # context (issue title/body — whoever wrote the Issue could embed any
        # instruction) while it is in scope here. The tokens are stored on
        # the task and travel to the execute job via the approval; the
        # worker's SearchSanitizer refuses any query containing one.
        from agent_platform.search.vocab import extract_forbidden_tokens

        search_forbidden_tokens = list(
            extract_forbidden_tokens(
                issue_title=issue_title,
                issue_body=issue_body,
                repo_root=self._repo_root,
            )
        )
        # A plan that names no files and does not require human input can
        # never publish: approve() derives allowed_paths from
        # proposed_changes, so an empty list makes every execution outcome a
        # guaranteed scope violation. Fail here instead of after approval.
        if not result.proposed_changes and not result.requires_human_input:
            self._repo.transition_task(
                task.task_id,
                TaskStatus.FAILED.value,
                reason="plan proposed no file changes",
                now=now,
            )
            msg = (
                f"plan for {task.task_id} proposed no file changes "
                "(empty proposed_changes, requires_human_input=false)"
            )
            raise AdapterContractError(msg)
        self._store_plan_artifact(result, plan_hash)
        self._repo.update_task_plan(
            task.task_id,
            plan_hash=plan_hash,
            base_branch=base_branch,
            base_sha=base_sha,
            search_forbidden_tokens=list(search_forbidden_tokens),
            to_status=TaskStatus.AWAITING_APPROVAL.value,
            now=self._now(),
            reason=_plan_recorded_reason(result),
        )
        final = self._repo.get_task(task.task_id)
        return PlanOutcome(task=final, plan=result, duplicate=False)

    def redo_plan(
        self,
        *,
        repository: str,
        issue_number: int,
        base_branch: str,
        base_sha: str,
        issue_title: str | None = None,
        issue_body: str | None = None,
        predecessor_task_id: str | None = None,
        feedback: str = "",
    ) -> PlanOutcome:
        """Comment-kick redo: create revision N+1 for an issue whose
        latest task COMPLETED or FAILED. FAILED stays terminal — the retry is
        a new revision, so the failed attempt and its reason remain in the
        audit trail (no in-place reset). The redo feedback (the comment body
        after the command) becomes part of the untrusted issue context — it
        is text a human wrote about the previous result, not a trusted
        instruction."""
        repository = repository.lower()
        self.check_redo(
            repository=repository, issue_number=issue_number,
            predecessor_task_id=predecessor_task_id,
        )

        context_body = issue_body or ""
        if feedback:
            context_body = (
                f"{context_body}\n\n"
                "## 追加指摘（redo コメントより、untrusted）\n\n" + feedback
            )
        return self._plan_new_revision(
            repository=repository, issue_number=issue_number,
            base_branch=base_branch, base_sha=base_sha, issue_title=issue_title,
            context_body=context_body, reason="redo requested",
        )

    def check_redo(
        self, *, repository: str, issue_number: int,
        predecessor_task_id: str | None = None,
    ) -> None:
        """Raise unless redo applies to the issue now. Read-only: the webhook
        calls it before acknowledging the command, redo_plan calls it again
        before planning."""
        repository = repository.lower()
        latest = self._repo.get_task_by_issue(repository, issue_number)
        if latest is None:
            msg = f"no previous task for {repository}#{issue_number}; use run"
            raise StateConflictError(msg)
        if predecessor_task_id is not None and latest.task_id != predecessor_task_id:
            # The caller named an explicit predecessor: it must be a task of
            # this issue (any revision) — verified by lookup + fields below.
            if not predecessor_task_id.startswith("task-"):
                msg = f"invalid predecessor task id: {predecessor_task_id!r}"
                raise StateConflictError(msg)
            # Explicit predecessor may be an older revision; find it.
            try:
                predecessor = self._repo.get_task(predecessor_task_id)
            except NotFoundError:
                predecessor = None
            if predecessor is None or predecessor.repository != repository or (
                predecessor.issue_number != issue_number
            ):
                msg = (
                    f"predecessor {predecessor_task_id!r} does not belong to "
                    f"{repository}#{issue_number}"
                )
                raise StateConflictError(msg)
        elif latest.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            msg = (
                f"latest task {latest.task_id} is {latest.status.value}, not "
                "COMPLETED or FAILED — redo requires a finished run"
            )
            raise TaskStateRejectedError(
                msg, task_id=latest.task_id, status=latest.status.value,
                needs_input=self._needs_input(latest),
            )

    def check_answer(self, *, repository: str, issue_number: int) -> None:
        """Raise unless the issue's latest plan waits for an answer.
        Read-only; called by the webhook before acknowledging the command."""
        self._task_awaiting_answer(repository.lower(), issue_number)

    def _task_awaiting_answer(
        self, repository: str, issue_number: int
    ) -> tuple[Task, PlanResult]:
        latest = self._repo.get_task_by_issue(repository, issue_number)
        if latest is None:
            msg = f"no task for {repository}#{issue_number}; comment run first"
            raise StateConflictError(msg)
        plan = self._load_plan(latest) if latest.plan_hash is not None else None
        if (
            latest.status is not TaskStatus.AWAITING_APPROVAL
            or plan is None
            or not plan.requires_human_input
        ):
            msg = (
                f"latest task {latest.task_id} is {latest.status.value} and "
                "is not waiting for an answer"
            )
            raise TaskStateRejectedError(
                msg, task_id=latest.task_id, status=latest.status.value,
            )
        return latest, plan

    def _needs_input(self, task: Task) -> bool:
        """The task is a plan waiting for a human answer."""
        return (
            task.status is TaskStatus.AWAITING_APPROVAL
            and task.plan_hash is not None
            and self._load_plan(task).requires_human_input
        )

    def latest_task_for_issue(self, *, repository: str, issue_number: int) -> Task | None:
        """The issue's latest revision (None when the issue has no task)."""
        return self._repo.get_task_by_issue(repository.lower(), issue_number)

    def answer_plan(
        self,
        *,
        repository: str,
        issue_number: int,
        base_branch: str,
        base_sha: str,
        answers: list[str],
        issue_title: str | None = None,
        issue_body: str | None = None,
    ) -> PlanOutcome:
        """`/agent-platform answer`: the issue's latest plan asked for human
        input (requires_human_input); re-plan as revision N+1 with the
        plan's questions and every answer so far as added context.

        The questioning task is AWAITING_APPROVAL (approval of it is refused
        — it names no files) and is CANCELLED here, superseded by the new
        revision. The questions (model output) and the answers (comment
        text) are both untrusted context, never instructions."""
        repository = repository.lower()
        answers = [a.strip() for a in answers if a.strip()]
        if not answers:
            msg = "answer is empty; write the answer after `/agent-platform answer`"
            raise StateConflictError(msg)
        latest, plan = self._task_awaiting_answer(repository, issue_number)
        questions = "\n".join(f"- {q}" for q in plan.questions)
        asked = plan.summary + (f"\n{questions}" if questions else "")
        replies = "\n\n".join(f"### 回答 {i}\n{a}" for i, a in enumerate(answers, 1))
        context_body = (
            f"{issue_body or ''}\n\n"
            "## 前回の plan が求めた確認事項（untrusted）\n\n" + asked + "\n\n"
            "## 人からの回答（answer コメントより、untrusted）\n\n" + replies
        )
        revision = self._repo.latest_revision(repository, issue_number) + 1
        self._repo.transition_task(
            latest.task_id, TaskStatus.CANCELLED.value,
            reason=f"answered; superseded by revision {revision}", now=self._now(),
        )
        return self._plan_new_revision(
            repository=repository, issue_number=issue_number,
            base_branch=base_branch, base_sha=base_sha, issue_title=issue_title,
            context_body=context_body, reason="answer received",
        )

    def _plan_new_revision(
        self,
        *,
        repository: str,
        issue_number: int,
        base_branch: str,
        base_sha: str,
        issue_title: str | None,
        context_body: str,
        reason: str,
    ) -> PlanOutcome:
        """Create revision N+1 for the issue and plan it with context_body
        (issue body + redo feedback or answers) as the untrusted context."""
        revision = self._repo.latest_revision(repository, issue_number) + 1
        task_id = derive_task_id(repository, issue_number, revision)
        now = self._now()
        task = Task(
            task_id=task_id,
            repository=repository,
            issue_number=issue_number,
            revision=revision,
            created_at=now,
            updated_at=now,
        )
        try:
            self._repo.insert_task(task)
        except sqlite3.IntegrityError as exc:
            msg = f"concurrent revision for {repository}#{issue_number} revision {revision}"
            raise IntegrityError(msg) from exc

        self._repo.transition_task(
            task.task_id, TaskStatus.PLANNING.value, reason=reason, now=now
        )
        job = PlanJobSpec(
            task_id=task.task_id,
            repository=task.repository,
            issue_number=task.issue_number,
            base_branch=base_branch,
            base_sha=base_sha,
            issue_title=issue_title,
            issue_body=context_body or None,
        )
        result = self._adapter.plan(job)
        if result.task_id != task.task_id or result.repository != task.repository:
            msg = (
                f"adapter echoed wrong task identity: {result.task_id!r}"
                f" != {task.task_id!r}"
            )
            self._repo.transition_task(
                task.task_id,
                TaskStatus.FAILED.value,
                reason="adapter contract violation",
                now=now,
            )
            raise AdapterContractError(msg)
        plan_hash = compute_plan_hash(result)
        if not result.proposed_changes and not result.requires_human_input:
            self._repo.transition_task(
                task.task_id,
                TaskStatus.FAILED.value,
                reason="plan proposed no file changes",
                now=now,
            )
            msg = (
                f"plan for {task.task_id} proposed no file changes "
                "(empty proposed_changes, requires_human_input=false)"
            )
            raise AdapterContractError(msg)
        from agent_platform.search.vocab import extract_forbidden_tokens

        search_forbidden_tokens = list(
            extract_forbidden_tokens(
                issue_title=issue_title,
                issue_body=context_body,
                repo_root=self._repo_root,
            )
        )
        # Same as plan(): approve/execute load the plan by digest, so a
        # revision without its artifact could never be approved.
        self._store_plan_artifact(result, plan_hash)
        self._repo.update_task_plan(
            task.task_id,
            plan_hash=plan_hash,
            base_branch=base_branch,
            base_sha=base_sha,
            search_forbidden_tokens=list(search_forbidden_tokens),
            to_status=TaskStatus.AWAITING_APPROVAL.value,
            now=self._now(),
            reason=_plan_recorded_reason(result),
        )
        final = self._repo.get_task(task.task_id)
        return PlanOutcome(task=final, plan=result, duplicate=False)

    # --- approve ---

    def resolve_approvable(
        self, *, repository: str, issue_number: int
    ) -> tuple[str, str]:
        """The task the no-args `/agent-platform approve` on this issue
        refers to: the issue's latest revision, when it is AWAITING_APPROVAL
        (approve it) or APPROVED (a replayed approval resumes a pipeline
        that never started). Scoped to the commented issue, so other issues'
        pending tasks never make the shorthand ambiguous — the human never
        needs the task id or the plan hash."""
        repository = repository.lower()
        task = self._repo.get_task_by_issue(repository, issue_number)
        if task is None:
            msg = f"no task for {repository}#{issue_number}; comment run first"
            raise StateConflictError(msg)
        if task.status not in (TaskStatus.AWAITING_APPROVAL, TaskStatus.APPROVED):
            msg = (
                f"latest task {task.task_id} is {task.status.value}, "
                "nothing to approve"
            )
            raise TaskStateRejectedError(
                msg, task_id=task.task_id, status=task.status.value,
            )
        if task.plan_hash is None:
            msg = f"task {task.task_id} is {task.status.value} without a plan_hash"
            raise PlanHashMismatchError(msg)
        if self._load_plan(task).requires_human_input:
            msg = (
                f"task {task.task_id} needs human input; reply with "
                "`/agent-platform answer <answer>`"
            )
            raise TaskStateRejectedError(
                msg, task_id=task.task_id, status=task.status.value,
                needs_input=True,
            )
        return task.task_id, task.plan_hash

    def approve(
        self,
        *,
        task_id: str,
        plan_hash: str,
        approver: str,
        ttl_hours: int | None = None,
    ) -> ApprovalOutcome:
        key = f"approve:{task_id}:{plan_hash}:{approver}"
        now = self._now()

        recorded = self._repo.get_idempotency(key)
        if recorded is not None:
            task = self._repo.get_task(task_id)
            approval = self._repo.find_approval(task_id, plan_hash)
            if approval is not None:
                return ApprovalOutcome(task=task, approval=approval, duplicate=True)

        with self._repo._tx() as cur:
            row = cur.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                msg = f"task not found: {task_id}"
                raise NotFoundError(msg)
            current = row["status"]
            if current != TaskStatus.AWAITING_APPROVAL.value:
                msg = f"task is {current}, not AWAITING_APPROVAL"
                raise StateConflictError(msg)
            recorded_hash = row["plan_hash"]
            if recorded_hash is None or recorded_hash != plan_hash:
                msg = f"plan_hash mismatch: task has {recorded_hash!r}"
                raise PlanHashMismatchError(msg)

        task = self._repo.get_task(task_id)
        existing = self._repo.find_approval(task_id, plan_hash)
        if existing is not None:
            if existing.approver == approver:
                self._repo.insert_idempotency(key, "approve", task_id, now, ignore=True)
                return ApprovalOutcome(task=task, approval=existing, duplicate=True)
            msg = f"task {task_id} already approved by {existing.approver}"
            raise ApprovalConflictError(msg)

        plan = self._load_plan(task)
        if plan.requires_human_input:
            # It names no files (allowed_paths would be empty): approving it
            # could only produce a guaranteed scope violation.
            msg = (
                f"plan for {task_id} needs human input; reply with "
                "`/agent-platform answer <answer>` instead of approving"
            )
            raise StateConflictError(msg)
        effective_ttl = (
            ttl_hours if ttl_hours is not None else self._settings.default_approval_ttl_hours
        )
        approved_at = self._clock.now()
        expires_at = approved_at + datetime.timedelta(hours=effective_ttl)
        approval = Approval(
            approval_id=derive_approval_id(task_id, plan_hash),
            task_id=task_id,
            plan_hash=plan_hash,
            repository=task.repository,
            base_branch=task.base_branch or "main",
            base_sha=task.base_sha or "",
            allowed_paths=[change.path for change in plan.proposed_changes],
            test_commands=list(plan.test_commands),
            # Derived at plan time from the untrusted task context
            # (issue title/body) and stored on the task; the worker's
            # SearchSanitizer refuses any search query containing one.
            search_forbidden_tokens=list(self._repo.get_task(task_id).search_forbidden_tokens),
            approver=approver,
            approved_at=approved_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._repo.insert_approval(approval)
        self._repo.transition_task(
            task_id, TaskStatus.APPROVED.value, reason=f"approved by {approver}", now=now
        )
        self._repo.insert_idempotency(key, "approve", task_id, now, ignore=True)
        return ApprovalOutcome(
            task=self._repo.get_task(task_id), approval=approval, duplicate=False
        )

    # --- reject ---

    def reject(
        self,
        *,
        task_id: str,
        plan_hash: str,
        approver: str,
        reason: str = "",
    ) -> RejectionOutcome:
        """Reject a plan awaiting approval. Records the rejecting identity in
        the audit trail (task_events) and moves the task to CANCELLED."""
        key = f"reject:{task_id}:{plan_hash}:{approver}"
        now = self._now()

        recorded = self._repo.get_idempotency(key)
        if recorded is not None:
            task = self._repo.get_task(task_id)
            return RejectionOutcome(task=task, duplicate=True)

        with self._repo._tx() as cur:
            row = cur.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                msg = f"task not found: {task_id}"
                raise NotFoundError(msg)
            current = row["status"]
            if current != TaskStatus.AWAITING_APPROVAL.value:
                msg = f"task is {current}, not AWAITING_APPROVAL"
                raise StateConflictError(msg)
            recorded_hash = row["plan_hash"]
            if recorded_hash is None or recorded_hash != plan_hash:
                msg = f"plan_hash mismatch: task has {recorded_hash!r}"
                raise PlanHashMismatchError(msg)

        detail = reason or "rejected"
        self._repo.transition_task(
            task_id,
            TaskStatus.CANCELLED.value,
            reason=f"rejected by {approver}: {detail}"[:200],
            now=now,
        )
        self._repo.insert_idempotency(key, "reject", task_id, now, ignore=True)
        return RejectionOutcome(task=self._repo.get_task(task_id), duplicate=False)

    # --- approval gate ---

    def require_active_approval(
        self,
        *,
        task_id: str,
        plan_hash: str,
        require_approved_state: bool = True,
    ) -> Approval:
        """The single gate any Execute Job creation must pass.

        Proof obligations:
        - no approval record for (task, plan_hash) -> ExpiredApprovalError is
          NOT enough; a missing record raises ApprovalError via
          PlanHashMismatchError if the task never got that hash, or
          StateConflictError if the task is not APPROVED.
        - an expired record -> ExpiredApprovalError, state stays APPROVED but
          no execute may proceed (caller must re-plan).
        Returns the active (unexpired) approval on success.
        """
        now = self._now()
        task = self._repo.get_task(task_id)
        if task.plan_hash is None or task.plan_hash != plan_hash:
            msg = f"plan_hash mismatch: task has {task.plan_hash!r}"
            raise PlanHashMismatchError(msg)
        approval = self._repo.find_approval(task_id, plan_hash)
        if approval is None:
            msg = f"no approval record for {task_id} with {plan_hash}"
            raise ExpiredApprovalError(msg)
        if approval_is_expired(approval, now):
            msg = f"approval {approval.approval_id} expired at {approval.expires_at}"
            raise ExpiredApprovalError(msg)
        if require_approved_state and task.status != TaskStatus.APPROVED:
            msg = f"task is {task.status.value}, not APPROVED"
            raise StateConflictError(msg)
        return approval

    # --- status ---

    def status(self, *, task_id: str) -> TaskStatusView:
        task = self._repo.get_task(task_id)
        approval = None
        approval_expired = False
        if task.plan_hash is not None:
            approval = self._repo.find_approval(task_id, task.plan_hash)
            if approval is not None:
                approval_expired = approval_is_expired(approval, self._now())
        plan = None
        if task.plan_hash is not None:
            try:
                plan = self._load_plan(task)
            except NotFoundError:
                plan = None
        execution = None
        if task.status in (TaskStatus.READY_TO_PUBLISH, TaskStatus.NEEDS_REAPPROVAL):
            try:
                execution = self._load_execution(task)
            except (NotFoundError, ValueError):
                execution = None
        events = self._repo.list_events(task_id)
        return TaskStatusView(
            task=task,
            approval=approval,
            approval_expired=approval_expired,
            plan=plan,
            execution=execution,
            events=events,
        )

    # --- execute ---

    def execute(
        self,
        *,
        task_id: str,
        plan_hash: str,
        base_branch: str | None = None,
        base_sha: str | None = None,
    ) -> "ExecutionOutcome":
        """Run the approved plan in a sandbox and record the ExecutionResult.

        The approval gate runs BEFORE the job launches: no approval
        record, an expired one, a hash mismatch, or a non-APPROVED state
        refuses the launch entirely (no Job is started on a plan hash or
        base SHA mismatch).
        """
        key = f"execute:{task_id}:{plan_hash}"
        now = self._now()

        recorded = self._repo.get_idempotency(key)
        if recorded is not None:
            # Idempotent replay BEFORE the gate: the execute already ran
            # under this (task, plan_hash); re-checking APPROVED would
            # wrongly refuse replays from READY_TO_PUBLISH.
            task = self._repo.get_task(task_id)
            execution = self._load_execution(task) if (
                task.status == TaskStatus.READY_TO_PUBLISH
            ) else None
            return ExecutionOutcome(task=task, execution=execution, duplicate=True)

        # The approval gate: proof that an unexpired approval record exists for
        # exactly this (task, plan_hash) and the task is APPROVED.
        self.require_active_approval(task_id=task_id, plan_hash=plan_hash)
        # The DAG dependency gate: parents must be complete first.
        self._require_dependencies_satisfied(task_id)
        task = self._repo.get_task(task_id)
        approval = self._repo.find_approval(task_id, plan_hash)
        if approval is None:  # defensive; the gate already proved otherwise
            msg = f"approval vanished for {task_id}"
            raise IntegrityError(msg)

        # base SHA/branch pinned by the approval, not by the caller.
        if base_sha is not None and base_sha != approval.base_sha:
            msg = (
                f"base_sha mismatch: approval froze {approval.base_sha!r}, "
                f"got {base_sha!r}"
            )
            raise PlanHashMismatchError(msg)
        if base_branch is not None and base_branch != approval.base_branch:
            msg = (
                f"base_branch mismatch: approval froze {approval.base_branch!r}, "
                f"got {base_branch!r}"
            )
            raise PlanHashMismatchError(msg)

        outcome = self._run_execution(
            task_id=task_id, repository=task.repository,
            plan_hash=plan_hash, approval=approval, attempt=1,
        )
        self._repo.insert_idempotency(key, "execute", task_id, now, ignore=True)
        return outcome

    def _run_execution(
        self,
        *,
        task_id: str,
        repository: str,
        plan_hash: str,
        approval: Approval,
        attempt: int,
    ) -> "ExecutionOutcome":
        """Shared execution path for execute() and reexecute(). The
        idempotency key carries the attempt number so a re-execute after a
        review finding is not swallowed by the first run's key."""
        now = self._now()
        self._repo.transition_task(
            task_id, TaskStatus.EXECUTING.value, reason="execute requested", now=now
        )
        job = ExecuteJobSpec(
            task_id=task_id,
            repository=repository,
            plan_hash=plan_hash,
            base_branch=approval.base_branch,
            base_sha=approval.base_sha,
            allowed_paths=approval.allowed_paths,
            test_commands=approval.test_commands,
            search_forbidden_tokens=list(approval.search_forbidden_tokens),
            plan_ref=f"artifact://{plan_hash.removeprefix('sha256:')}",
        )
        try:
            result = self._adapter.execute(job)
        except Exception:
            self._repo.transition_task(
                task_id, TaskStatus.FAILED.value, reason="execute failed",
                now=self._now(),
            )
            raise
        if (
            result.task_id != task_id
            or result.plan_hash != plan_hash
            or result.base_sha != approval.base_sha
        ):
            self._repo.transition_task(
                task_id, TaskStatus.FAILED.value,
                reason="execute contract violation", now=self._now(),
            )
            msg = f"execute echoed wrong identity for {task_id}"
            raise AdapterContractError(msg)

        execution_ref = self._store_execution_artifact(result)
        next_status = (
            TaskStatus.READY_TO_PUBLISH
            if result.status == "succeeded" and not result.violations
            else TaskStatus.NEEDS_REAPPROVAL if result.status == "succeeded"
            else TaskStatus.FAILED
        )
        reason = "execution recorded"
        if result.violations:
            reason = f"violations: {', '.join(v.kind for v in result.violations)}"
        self._repo.update_task_execution(
            task_id,
            execution_ref=execution_ref.uri,
            to_status=next_status.value,
            now=self._now(),
            reason=reason,
        )
        self._repo.insert_idempotency(
            f"execute:{task_id}:{plan_hash}:attempt{attempt}",
            "execute", task_id, now, ignore=True,
        )
        return ExecutionOutcome(
            task=self._repo.get_task(task_id), execution=result, duplicate=False
        )

    def _store_execution_artifact(self, result: ExecutionResult) -> ArtifactRef:
        canonical = json.dumps(
            result.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        ref = self._artifacts.put(_bytes_artifact(canonical))
        self._repo.insert_artifact_metadata(
            ref.digest, len(canonical), _AUDIT_RETENTION, self._now()
        )
        return ref

    # --- replan / reexecute ---

    def replan(self, *, task_id: str, plan_hash: str, approver: str) -> Task:
        """Return a NEEDS_REAPPROVAL task to PLANNING so a Plan Job can
        revise the plan. The old approval becomes irrelevant: a revised plan
        has a new plan_hash and needs a fresh approval. Records who chose
        to re-plan (audit)."""
        now = self._now()
        task = self._repo.get_task(task_id)
        if task.plan_hash != plan_hash:
            msg = f"plan_hash mismatch: task has {task.plan_hash!r}"
            raise PlanHashMismatchError(msg)
        if task.status != TaskStatus.NEEDS_REAPPROVAL:
            msg = f"task is {task.status.value}, not NEEDS_REAPPROVAL"
            raise StateConflictError(msg)
        self._repo.transition_task(
            task_id, TaskStatus.PLANNING.value,
            reason=f"replan requested by {approver}", now=now,
        )
        return self._repo.get_task(task_id)

    def reopen(
        self, *, task_id: str, plan_hash: str, requested_by: str, reason: str
    ) -> Task:
        """Send a READY_TO_PUBLISH task back to NEEDS_REAPPROVAL when a
        human rejects the execution before publish (e.g. an empty or wrong
        patch). From there the existing replan / reexecute paths apply —
        this replaces a manual direct SQL transition. Records who reopened
        it and why (audit)."""
        task = self._repo.get_task(task_id)
        if task.plan_hash != plan_hash:
            msg = f"plan_hash mismatch: task has {task.plan_hash!r}"
            raise PlanHashMismatchError(msg)
        if task.status not in (TaskStatus.READY_TO_PUBLISH, TaskStatus.NEEDS_REAPPROVAL):
            msg = f"task is {task.status.value}, not READY_TO_PUBLISH"
            raise StateConflictError(msg)
        # NEEDS_REAPPROVAL is accepted as an idempotent replay
        # (ALREADY_APPLIED in the repository's CAS).
        self._repo.transition_task(
            task_id, TaskStatus.NEEDS_REAPPROVAL.value,
            reason=f"reopened by {requested_by}: {reason}", now=self._now(),
        )
        return self._repo.get_task(task_id)

    def reexecute(
        self,
        *,
        task_id: str,
        plan_hash: str,
        requested_by: str,
    ) -> "ExecutionOutcome":
        """Re-run the Execute Job for an unchanged, still-approved plan.

        Used when a review found implementation-only defects: the plan (and
        its plan_hash) is unchanged, so the existing approval stays valid —
        the active-approval gate re-checks expiry. Refuses when the task is
        not in NEEDS_REAPPROVAL (that is the only state where re-execution
        is meaningful) or when the approval expired.
        """
        task = self._repo.get_task(task_id)
        if task.plan_hash != plan_hash:
            msg = f"plan_hash mismatch: task has {task.plan_hash!r}"
            raise PlanHashMismatchError(msg)
        if task.status != TaskStatus.NEEDS_REAPPROVAL:
            msg = f"task is {task.status.value}, not NEEDS_REAPPROVAL"
            raise StateConflictError(msg)
        # The approval must still be unexpired (no new approval needed).
        self.require_active_approval(
            task_id=task_id, plan_hash=plan_hash, require_approved_state=False
        )
        approval = self._repo.find_approval(task_id, plan_hash)
        if approval is None:
            msg = f"approval vanished for {task_id}"
            raise IntegrityError(msg)
        return self._run_execution(
            task_id=task_id, repository=task.repository,
            plan_hash=plan_hash, approval=approval, attempt=2,
        )

    # --- contracts ---

    def publish_contract(
        self, *, contract: "ContractArtifact",
    ) -> "ContractPublishOutcome":
        """Publish a new contract version as a content-addressed artifact.

        A contract change is published as an artifact first; consumers then
        reference that version. Publishing a (name, version) pair that already exists
        with DIFFERENT bytes is an integrity error — versions are immutable.
        Same bytes is an idempotent replay.
        """
        key = f"contract:{contract.name}:{contract.version}"
        now = self._now()
        canonical = contract.canonical_bytes()
        digest = hashlib.sha256(canonical).hexdigest()

        recorded = self._repo.get_idempotency(key)
        existing = self._repo.get_contract_version(
            name=contract.name, version=contract.version
        )
        if recorded is not None or existing is not None:
            if existing is not None and existing["digest"] != digest:
                msg = (
                    f"contract {contract.name}@{contract.version} already "
                    f"exists with different content (digest {existing['digest'][:12]}); "
                    "versions are immutable — publish a new version"
                )
                raise IntegrityError(msg)
            return ContractPublishOutcome(
                name=contract.name, version=contract.version,
                digest=digest, duplicate=True,
            )

        ref = self._artifacts.put(_bytes_artifact(canonical))
        if ref.digest != digest:
            msg = (
                f"contract artifact digest {ref.digest} != computed {digest}"
            )
            raise IntegrityError(msg)
        self._repo.insert_artifact_metadata(
            digest, len(canonical), _AUDIT_RETENTION, now
        )
        self._repo.insert_contract_version(
            name=contract.name, version=contract.version,
            publisher_task_id=contract.publisher_task_id,
            digest=digest, now=now,
        )
        self._repo.insert_idempotency(key, "execute", contract.publisher_task_id,
                                      now, ignore=True)
        return ContractPublishOutcome(
            name=contract.name, version=contract.version,
            digest=digest, duplicate=False,
        )

    def pin_contract(
        self, *, contract_name: str, version: str, consumer_task_id: str,
    ) -> "ContractPinOutcome":
        """Pin a consumer task to an exact contract version. The pin stores
        the digest at pin time — a later digest change for the same version
        would be an integrity error caught by publish_contract."""
        record = self._repo.get_contract_version(
            name=contract_name, version=version
        )
        if record is None:
            msg = f"contract version not found: {contract_name}@{version}"
            raise NotFoundError(msg)
        self._repo.get_task(consumer_task_id)  # existence check
        self._repo.insert_consumer_pin(
            contract_name=contract_name, version=version,
            digest=record["digest"], consumer_task_id=consumer_task_id,
            now=self._now(),
        )
        return ContractPinOutcome(
            contract_name=contract_name, version=version,
            digest=record["digest"], consumer_task_id=consumer_task_id,
        )

    def contract_consumers(self, *, contract_name: str) -> list[dict[str, str]]:
        """Who pins which version — the parent aggregation's 契約 trace."""
        return self._repo.list_consumer_pins(contract_name=contract_name)

    # --- task DAG ---

    def attach_child(
        self,
        *,
        parent_task_id: str,
        child_task_id: str,
        approver: str,
    ) -> DagEdgeOutcome:
        """Record that child_task is part of parent's multi-repo job.

        The parent task must exist; the child must exist too (it was created
        by a normal single-repo plan). Both identities are recorded in the
        audit trail with the approving identity — a human is required
        to approve the repo set and the dependency structure.
        Cycle detection: the child must not already be an ancestor of the
        parent (a parent->child edge that closes a cycle).
        """
        key = f"dag:{parent_task_id}:{child_task_id}:{approver}"
        now = self._now()

        recorded = self._repo.get_idempotency(key)
        if recorded is not None:
            return DagEdgeOutcome(
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                duplicate=True,
            )

        self._repo.get_task(parent_task_id)  # NotFoundError if missing
        child = self._repo.get_task(child_task_id)
        if child.repository == "":
            msg = "child has no repository"  # defensive; model enforces
            raise IntegrityError(msg)

        # Cycle check: an edge P->C means "C waits for P". It creates a
        # cycle iff P is already reachable from C by following child edges
        # (i.e., P waits transitively for C).
        seen: set[str] = set()
        frontier = [child_task_id]
        while frontier:
            current = frontier.pop()
            if current == parent_task_id:
                msg = (
                    f"dag edge refused: {parent_task_id} already waits "
                    f"transitively for {child_task_id}; the edge would create "
                    "a cycle"
                )
                raise StateConflictError(msg)
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(self._repo.list_children(current))

        self._repo.insert_dag_edge(
            parent_task_id, child_task_id, created_by=approver, now=now
        )
        self._repo.insert_idempotency(
            key, "approve", parent_task_id, now, ignore=True
        )
        return DagEdgeOutcome(
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            duplicate=False,
        )

    def parent_status(self, *, parent_task_id: str) -> ParentStatusView:
        """Project the parent's state from its children (never stored)."""
        children = self._repo.list_children(parent_task_id)
        if not children:
            msg = f"no children recorded for {parent_task_id}"
            raise NotFoundError(msg)
        rows: list[dict[str, str]] = []
        all_done = True
        any_failed = False
        any_started = False
        failed_ids: set[str] = set()
        for child_id in children:
            child = self._repo.get_task(child_id)
            started = child.status.value not in (
                TaskStatus.RECEIVED.value, TaskStatus.PLANNING.value,
                TaskStatus.AWAITING_APPROVAL.value, TaskStatus.APPROVED.value,
            )
            done = child.status.value in (
                # The child's own work is finished once its artifact is
                # final (READY_TO_PUBLISH) or it has been published/reviewed.
                TaskStatus.READY_TO_PUBLISH.value,
                TaskStatus.PR_CREATED.value,
                TaskStatus.REVIEWING.value,
                TaskStatus.COMPLETED.value,
            )
            failed = child.status.value == TaskStatus.FAILED.value
            any_started = any_started or started
            all_done = all_done and done
            any_failed = any_failed or failed
            rows.append(
                {
                    "task_id": child_id,
                    "repository": child.repository,
                    "status": child.status.value,
                }
            )
        if all_done:
            projected = "COMPLETED"
        elif any_failed:
            projected = "FAILED"
        elif any_started:
            projected = "IN_PROGRESS"
        else:
            projected = "NOT_STARTED"
        # Rollback material: which failed children block which
        # followers, and whether failed children hold contract pins.
        blocked_followers: dict[str, list[str]] = {}
        for failed_id in sorted(failed_ids):
            followers = [
                candidate
                for candidate in children
                if failed_id in self._repo.list_dependencies(candidate)
                and candidate != failed_id
            ]
            if followers:
                blocked_followers[failed_id] = followers
        failed_with_pins = []
        for failed_id in sorted(failed_ids):
            failed_repo = self._repo.get_task(failed_id).repository
            for pin in self._repo.list_consumer_pins_by_publisher(
                publisher_task_id=failed_id
            ):
                failed_with_pins.append(
                    {"task_id": failed_id, "repository": failed_repo,
                     "contract": pin["contract_name"],
                     "version": pin["version"]}
                )
        return ParentStatusView(
            parent_task_id=parent_task_id,
            children=rows,
            projected_status=projected,
            all_children_complete=all_done,
            blocked_followers=blocked_followers,
            failed_with_pins=failed_with_pins,
        )

    def _require_dependencies_satisfied(self, task_id: str) -> None:
        """Dependency gate: every recorded parent that has actually executed
        something must have reached PR_CREATED-or-beyond before this task may
        execute/publish. A parent that never executed (a coordination shell
        for a multi-repo request) imposes no ordering — the DAG records
        grouping, and real ordering comes from executed dependencies.
        Refusal is a domain rejection — no auto-retry."""
        for parent_id in self._repo.list_dependencies(task_id):
            parent = self._repo.get_task(parent_id)
            if parent.execution_ref is None:
                continue  # shell parent: no ordering constraint
            ok = parent.status.value in (
                TaskStatus.READY_TO_PUBLISH.value,
                TaskStatus.PR_CREATED.value,
                TaskStatus.REVIEWING.value,
                TaskStatus.COMPLETED.value,
            )
            if not ok:
                msg = (
                    f"dependency not satisfied: parent {parent_id} is "
                    f"{parent.status.value}; this task must wait"
                )
                raise DependencyNotSatisfiedError(msg)

    # --- review ---

    def review(
        self,
        *,
        task_id: str,
        plan_hash: str,
        diff: str,
        head_sha: str,
    ) -> ReviewOutcome:
        """Run the independent review agent over the published diff.

        Gate: the task must have a recorded PR (PR_CREATED state) — the
        reviewer examines the diff of what was published. The ReviewResult
        is stored as an audit artifact and the task moves to REVIEWING.
        The review itself cannot change code: it runs in a read-only
        sandbox with no credentials.
        """
        key = f"review:{task_id}:{plan_hash}"
        now = self._now()

        recorded = self._repo.get_idempotency(key)
        if recorded is not None:
            task = self._repo.get_task(task_id)
            return ReviewOutcome(task=task, review=None, duplicate=True)

        task = self._repo.get_task(task_id)
        if task.plan_hash != plan_hash:
            msg = f"plan_hash mismatch: task has {task.plan_hash!r}"
            raise PlanHashMismatchError(msg)
        if task.status != TaskStatus.PR_CREATED:
            msg = f"task is {task.status.value}, not PR_CREATED"
            raise StateConflictError(msg)
        if task.execution_ref is None:
            msg = f"task {task_id} has no execution to review"
            raise StateConflictError(msg)
        self._load_execution(task)  # must exist and parse — audit integrity
        approval = self._repo.find_approval(task_id, plan_hash)
        if approval is None:
            msg = f"approval vanished for {task_id}"
            raise IntegrityError(msg)
        self._load_plan(task)  # stored plan must agree with the task record

        job = ReviewJobSpec(
            task_id=task_id,
            repository=task.repository,
            plan_hash=plan_hash,
            base_sha=approval.base_sha,
            head_sha=head_sha,
        )
        self._repo.transition_task(
            task_id, TaskStatus.REVIEWING.value, reason="review requested", now=now
        )
        try:
            # The adapter receives context through the wiring (head_sha
            # provider supplies the PR head commit).
            review = self._adapter.review(job)
        except Exception:
            self._repo.transition_task(
                task_id, TaskStatus.FAILED.value, reason="review failed",
                now=self._now(),
            )
            raise
        if review.task_id != task_id or review.repository != task.repository:
            msg = f"review echoed wrong identity for {task_id}"
            raise AdapterContractError(msg)

        # Evidence validation: every finding must quote diff lines that
        # exist in the reviewed diff. Findings with absent evidence are
        # recorded as unsupported (they do not block — the hallucination
        # defense) and are visible in the audit artifact.
        unsupported = [
            f for f in review.findings if f.evidence and f.evidence not in diff
        ]
        if unsupported:
            logger.warning(
                "review findings with evidence absent from diff "
                "(recorded, not blocking): %s",
                [f.message[:80] for f in unsupported],
            )

        canonical = json.dumps(
            review.model_dump(mode="json"),
            sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        ref = self._artifacts.put(_bytes_artifact(canonical))
        self._repo.insert_artifact_metadata(
            ref.digest, len(canonical), _AUDIT_RETENTION, self._now()
        )
        # Review outcome routing: the findings decide where the task
        # goes next.
        #  - no findings / only low+info  -> stays REVIEWING then COMPLETED:
        #    the PR is a Draft; a human reviews it in the normal GitHub flow.
        #  - plan-level problems (medium+) -> NEEDS_REAPPROVAL: the plan must
        #    be revised in a Plan Job and re-approved (plan_hash changes).
        #  - implementation-only problems (medium+) -> back to
        #    READY_TO_PUBLISH? No: the executed code has defects, so the
        #    outcome is NEEDS_REAPPROVAL as well unless the caller forces an
        #    execute retry. The routing below distinguishes via the review's
        #    severity: medium+ always goes to NEEDS_REAPPROVAL (human picks
        #    re-plan or re-execute); low/info is not a blocker.
        blocking = [
            f for f in review.findings
            if f.severity in ("critical", "high", "medium")
            and f not in unsupported
        ]
        unsupported_note = (
            f", {len(unsupported)} unsupported evidence" if unsupported else ""
        )
        if blocking:
            self._repo.transition_task(
                task_id, TaskStatus.NEEDS_REAPPROVAL.value,
                reason=f"review findings: {len(blocking)} blocking "
                f"(critical/high/medium){unsupported_note}",
                now=self._now(),
            )
        else:
            self._repo.transition_task(
                task_id, TaskStatus.COMPLETED.value,
                reason=f"review clean ({len(review.findings)} non-blocking)"
                f"{unsupported_note}",
                now=self._now(),
            )
        self._repo.insert_idempotency(key, "execute", task_id, now, ignore=True)
        return ReviewOutcome(
            task=self._repo.get_task(task_id), review=review, duplicate=False
        )

    # --- publish ---

    def publish(
        self,
        *,
        task_id: str,
        plan_hash: str,
        patch: str,
    ) -> "PublishOutcome":
        """Create a Draft PR from a verified execution result.

        Re-verification order: task READY_TO_PUBLISH and its
        recorded execution_ref -> patch digest matches the stored execution
        (the artifact IS the patch source of truth via changed_files) ->
        approval gate (active approval) -> publisher-side structural patch
        checks -> GitHub writes.
        """
        key = f"publish:{task_id}:{plan_hash}"
        now = self._now()

        recorded = self._repo.get_idempotency(key)
        if recorded is not None:
            task = self._repo.get_task(task_id)
            return PublishOutcome(task=task, duplicate=True, pr_url=None)

        task = self._repo.get_task(task_id)
        if task.plan_hash != plan_hash:
            msg = f"plan_hash mismatch: task has {task.plan_hash!r}"
            raise PlanHashMismatchError(msg)
        if task.status != TaskStatus.READY_TO_PUBLISH:
            msg = f"task is {task.status.value}, not READY_TO_PUBLISH"
            raise StateConflictError(msg)
        self._require_dependencies_satisfied(task_id)
        if self._publisher is None:
            msg = "no publisher configured (fake adapter flow has no GitHub)"
            raise StateConflictError(msg)

        execution = self._load_execution(task)
        if execution.status != "succeeded" or execution.violations:
            msg = "execution has violations or did not succeed; publish refused"
            raise StateConflictError(msg)

        # The approval gate re-check (approval still unexpired). The task is
        # READY_TO_PUBLISH at this point, so the APPROVED-state check does
        # not apply — the earlier execution already passed it.
        self.require_active_approval(
            task_id=task_id, plan_hash=plan_hash, require_approved_state=False
        )
        approval = self._repo.find_approval(task_id, plan_hash)
        if approval is None:
            msg = f"approval vanished for {task_id}"
            raise IntegrityError(msg)

        # Allowlist check with the REAL approval paths (publisher does the
        # structural checks separately).
        from agent_platform.publisher.git_ops import check_patch

        check_patch(
            patch,
            changed_files=execution.changed_files,
            allowed_paths=approval.allowed_paths,
        )

        plan = self._load_plan(task)
        request = PublishRequest(
            task_id=task_id,
            repository=task.repository,
            plan_hash=plan_hash,
            base_branch=approval.base_branch,
            base_sha=approval.base_sha,
            patch=patch,
            changed_files=execution.changed_files,
            approver=approval.approver,
            approval_id=approval.approval_id,
            approved_at=approval.approved_at,
            expires_at=approval.expires_at,
            pr_body_draft=execution.pr_body_draft,
            issue_number=task.issue_number,
            plan_summary=plan.summary[:200] if plan.summary else None,
        )
        result = self._publisher.publish(request)
        self._repo.transition_task(
            task_id, TaskStatus.PR_CREATED.value,
            reason=f"draft PR #{result.pr_number} created" if result.pr_number
            else "draft PR already existed",
            now=self._now(),
        )
        self._repo.insert_idempotency(key, "execute", task_id, now, ignore=True)
        return PublishOutcome(
            task=self._repo.get_task(task_id),
            duplicate=result.duplicate,
            pr_url=result.pr_url,
            pr_number=result.pr_number,
        )

    def _load_execution(self, task: Task) -> ExecutionResult:
        if task.execution_ref is None:
            msg = f"task {task.task_id} has no execution_ref"
            raise NotFoundError(msg)
        ref = ArtifactRef(uri=task.execution_ref)
        artifact = self._artifacts.get(ref)
        return ExecutionResult.model_validate_json(artifact.data)


def _verify_hash_bytes(plan: PlanResult, expected: str) -> bool:
    return compute_plan_hash(plan) == expected


def _bytes_artifact(data: bytes) -> Artifact:
    return Artifact(data=data)


def _plan_recorded_reason(plan: PlanResult) -> str:
    """task_events reason when a plan lands in AWAITING_APPROVAL: a plan
    that asks for human input is visibly different in the audit trail."""
    return "plan needs human input" if plan.requires_human_input else "plan recorded"
