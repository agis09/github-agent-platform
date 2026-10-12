"""GitHub-side reporting: publish plan/execute/review outcomes as PR
comments and check runs.

Content boundary (docs/design.md, enforced by the types below):
only structured, schema-backed fields are posted. Free-text from untrusted
sources (issue body, codex output, command stdout) never reaches these
functions — the builders take PlanResult / ExecutionResult / ReviewResult
models and render fixed-format markdown from their fields alone. Detailed
artifacts are referenced by digest, not inlined.

Idempotency: the check-run name is derived from the task id, so re-running
review updates the same check instead of piling up new ones. PR comments
are matched by an identifying prefix before posting a duplicate.
"""

from __future__ import annotations

import re

from agent_platform.adapters.github.reader import GitHubApiClient
from agent_platform.domain.models import ExecutionResult, PlanResult, ReviewResult

_CHECK_NAME = "agent-platform/review"
_COMMENT_PREFIX = "<!-- agent-platform:result:"
_PR_CREATED_PREFIX = "<!-- agent-platform:pr-created:"
_REVIEW_PREFIX = "<!-- agent-platform:review:"
_MAX_BODY_CHARS = 4096  # conservative; bodies below are bounded by schema


class GitHubReporter:
    """Post structured pipeline outcomes to GitHub. Uses the Publisher App's
    client (Issues: write for comments, Checks: write for check runs)."""

    def __init__(self, client: GitHubApiClient) -> None:
        self._client = client

    # --- publish: PR comment with the plan outline ---

    def post_plan_comment(self, *, repository: str, pr_number: int,
                          plan: PlanResult, plan_hash: str,
                          awaiting_human: bool = False, network: str = "") -> int:
        """Post the plan summary as a comment on a PR or an issue (both use
        the issues comments API). Returns the comment id. Skips (returns -1)
        when an identical comment already exists in that thread.

        awaiting_human=True is the issue-side post right after planning: the
        footer tells the human what to reply (approve, or answer when the
        plan asked for input). On the Draft PR the plan is already approved,
        so no reply prompt is added. network is the deployment's execute
        egress level, shown so the approver knows what execute may reach."""
        marker = f"{_COMMENT_PREFIX}{plan_hash}-->"
        existing = self._client.get(f"/repos/{repository}/issues/{pr_number}/comments")
        for comment in existing if isinstance(existing, list) else []:
            if isinstance(comment, dict) and str(comment.get("body", "")).startswith(marker):
                return -1  # already posted (idempotent re-publish)
        body = _render_plan_comment(
            plan=plan, plan_hash=plan_hash, marker=marker, awaiting_human=awaiting_human,
            network=network,
        )
        resp = self._client.post(
            f"/repos/{repository}/issues/{pr_number}/comments", {"body": body}
        )
        return int(resp["id"]) if isinstance(resp, dict) else -1

    # --- review: check run ---

    def post_review_check(self, *, repository: str, pr_number: int,
                          head_sha: str, review: ReviewResult,
                          plan_hash: str) -> int:
        """Create (or update, by name) the review check run on the PR head.
        Returns the check run id."""
        findings = list(review.findings)
        conclusion = "success" if not findings else "neutral"
        body = _render_check_output(review=review, plan_hash=plan_hash)
        payload = {
            "name": _check_name(repository, pr_number),
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": f"agent-platform review: {len(findings)} finding(s)",
                "summary": (review.summary or "No summary provided by the reviewer.")[:1000],
                "text": body[:_MAX_BODY_CHARS],
            },
        }
        resp = self._client.post(
            f"/repos/{repository}/check-runs", payload
        )
        return int(resp["id"]) if isinstance(resp, dict) else -1

    # --- review: PR comment with the review content ---

    def post_review_comment(self, *, repository: str, pr_number: int,
                            head_sha: str, review: ReviewResult,
                            plan_hash: str) -> int:
        """Post the review summary and findings as a PR comment, so they are
        read on the PR itself (the check run stays as the status signal).
        Skips (returns -1) when this head was already reviewed in the
        thread; a review of a new head posts a new comment."""
        if not _HEAD_SHA.fullmatch(head_sha):
            msg = f"unexpected head sha: {head_sha!r}"
            raise ValueError(msg)
        marker = f"{_REVIEW_PREFIX}{review.task_id}:{head_sha}-->"
        existing = self._client.get(f"/repos/{repository}/issues/{pr_number}/comments")
        for comment in existing if isinstance(existing, list) else []:
            if isinstance(comment, dict) and str(comment.get("body", "")).startswith(marker):
                return -1
        body = _render_review_comment(
            review=review, plan_hash=plan_hash, head_sha=head_sha, marker=marker
        )
        resp = self._client.post(
            f"/repos/{repository}/issues/{pr_number}/comments", {"body": body}
        )
        return int(resp["id"]) if isinstance(resp, dict) else -1

    # --- execute: failure as a failing check (never hide a failure) ---

    def post_execution_check(self, *, repository: str, pr_number: int,
                             head_sha: str, execution: ExecutionResult,
                             plan_hash: str) -> int:
        """Post an execution outcome check run. Violations or failed tests
        produce a failing check (conclusion='failure')."""
        failed = [t for t in execution.tests if t.status != "passed"]
        violations = list(execution.violations)
        conclusion = "success" if not failed and not violations else "failure"
        lines = [
            f"task: `{execution.task_id}`",
            f"plan: `{plan_hash}`",
            f"changed files: {', '.join(f'`{p}`' for p in execution.changed_files) or '(none)'}",
            "",
            "| test command | status |",
            "|---|---|",
        ]
        for t in execution.tests:
            lines.append(f"| `{t.command[:80]}` | {t.status} |")
        if violations:
            lines.append("")
            lines.append("**violations:**")
            lines += [f"- `{v.kind}`: {v.detail[:120]}" for v in violations]
        resp = self._client.post(
            f"/repos/{repository}/check-runs",
            {
                "name": _check_name(repository, pr_number) + "/execute",
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {
                    "title": f"agent-platform execute: {conclusion}",
                    "summary": (
                        f"{len(failed)} failed test command(s), "
                        f"{len(violations)} violation(s)"
                    ),
                    "text": "\n".join(lines)[:_MAX_BODY_CHARS],
                },
            },
        )
        return int(resp["id"]) if isinstance(resp, dict) else -1

    # --- publish: tell the issue the Draft PR exists ---

    def post_pr_created(self, *, repository: str, issue_number: int,
                        task_id: str, pr_number: int) -> int:
        """Issue comment linking the Draft PR, so the human who approved on
        the issue learns where the change landed. Fixed vocabulary only
        (task id, PR number); `#N` is autolinked by GitHub. Skips (returns
        -1) when this task's notice is already in the thread (re-publish)."""
        if not _TASK_ID.fullmatch(task_id):
            msg = f"unexpected task id: {task_id!r}"
            raise ValueError(msg)
        if pr_number <= 0:
            msg = f"unexpected PR number: {pr_number!r}"
            raise ValueError(msg)
        marker = f"{_PR_CREATED_PREFIX}{task_id}-->"
        existing = self._client.get(f"/repos/{repository}/issues/{issue_number}/comments")
        for comment in existing if isinstance(existing, list) else []:
            if isinstance(comment, dict) and str(comment.get("body", "")).startswith(marker):
                return -1
        body = (
            f"{marker}\n"
            f"agent-platform: Draft PR #{pr_number} was created for `{task_id}`."
        )
        resp = self._client.post(
            f"/repos/{repository}/issues/{issue_number}/comments", {"body": body}
        )
        return int(resp["id"]) if isinstance(resp, dict) else -1

    # --- plan: tell the issue the plan failed ---

    def post_plan_failed(self, *, repository: str, issue_number: int,
                         task_id: str) -> int:
        """Issue comment when a plan was rejected (empty plan). Fixed
        vocabulary only — the orchestrator's error text is not posted."""
        if not _TASK_ID.fullmatch(task_id):
            msg = f"unexpected task id: {task_id!r}"
            raise ValueError(msg)
        body = (
            f"agent-platform: the plan for `{task_id}` proposed no file changes "
            "and was not recorded.\n\n"
            "Reply with `/agent-platform redo` to plan again (text after the "
            "command is passed to the planner as extra guidance)."
        )
        resp = self._client.post(
            f"/repos/{repository}/issues/{issue_number}/comments", {"body": body}
        )
        return int(resp["id"]) if isinstance(resp, dict) else -1

    # --- webhook: acknowledge an accepted /agent-platform command ---

    def post_command_ack(self, *, repository: str, issue_number: int,
                         command: str, author: str, task_id: str | None = None,
                         auto_pipeline: bool = True) -> int:
        """Issue comment right after a command passed the permission gate,
        so the human sees it arrived before the (slow) plan / execute ends.
        Fixed vocabulary only: the command word, the author login and the
        task id are validated; the comment text itself is never echoed."""
        if command not in _ACK_NEXT:
            msg = f"unexpected command: {command!r}"
            raise ValueError(msg)
        if not _LOGIN.fullmatch(author):
            msg = f"unexpected login: {author!r}"
            raise ValueError(msg)
        if task_id is not None and not _TASK_ID.fullmatch(task_id):
            msg = f"unexpected task id: {task_id!r}"
            raise ValueError(msg)
        next_step = _ACK_NEXT[command]
        if command == "approve" and not auto_pipeline:
            next_step = "Run `agentctl execute` to continue."
        target = f" for `{task_id}`" if task_id else ""
        body = (
            f"agent-platform: accepted `/agent-platform {command}`{target} "
            f"from @{author}. {next_step}"
        )
        resp = self._client.post(
            f"/repos/{repository}/issues/{issue_number}/comments", {"body": body}
        )
        return int(resp["id"]) if isinstance(resp, dict) else -1

    # --- webhook: tell the issue a permitted command was refused ---

    def post_command_rejected(self, *, repository: str, issue_number: int,
                              command: str, author: str,
                              task_id: str | None = None,
                              status: str | None = None,
                              needs_input: bool = False) -> int:
        """Issue comment when a domain rule refused a permitted author's
        command, so a refusal is never silent. Fixed vocabulary only: the
        task id and status come from the task record (validated), never
        the exception text. A known task state gets a command-specific next
        step; any other refusal points at the orchestrator log."""
        if command not in _REJECT_NEXT:
            msg = f"unexpected command: {command!r}"
            raise ValueError(msg)
        if not _LOGIN.fullmatch(author):
            msg = f"unexpected login: {author!r}"
            raise ValueError(msg)
        if task_id is not None and not _TASK_ID.fullmatch(task_id):
            msg = f"unexpected task id: {task_id!r}"
            raise ValueError(msg)
        if status is not None and not _STATUS_WORD.fullmatch(status):
            msg = f"unexpected status: {status!r}"
            raise ValueError(msg)
        state = ""
        next_step = "The reason is recorded in the orchestrator log."
        if task_id is not None and status is not None:
            state = f" The latest task `{task_id}` is `{status}`."
            next_step = _NEEDS_INPUT_NEXT if needs_input else _REJECT_NEXT[command]
        body = (
            f"agent-platform: `/agent-platform {command}` from @{author} "
            f"was not applied.{state} {next_step}"
        )
        resp = self._client.post(
            f"/repos/{repository}/issues/{issue_number}/comments", {"body": body}
        )
        return int(resp["id"]) if isinstance(resp, dict) else -1

    # --- pipeline: tell the issue the post-approval run stopped ---

    def post_pipeline_stopped(self, *, repository: str, issue_number: int,
                              task_id: str, stage: str, status: str) -> int:
        """Issue comment when the post-approval pipeline stops before a
        clean review. Fixed vocabulary only (task id, stage, status) — no
        exception text, no codex output."""
        if stage not in _PIPELINE_STAGES or not _STATUS_WORD.fullmatch(status):
            msg = f"unexpected pipeline stage/status: {stage!r} {status!r}"
            raise ValueError(msg)
        if not _TASK_ID.fullmatch(task_id):
            msg = f"unexpected task id: {task_id!r}"
            raise ValueError(msg)
        body = (
            f"agent-platform: `{task_id}` stopped at **{stage}** "
            f"(status `{status}`).\n\n"
            f"Inspect with `agentctl status --task-id {task_id}`. "
            "Recovery: `agentctl reexecute` / `replan` for NEEDS_REAPPROVAL, "
            "`/agent-platform redo` for FAILED."
        )
        resp = self._client.post(
            f"/repos/{repository}/issues/{issue_number}/comments", {"body": body}
        )
        return int(resp["id"]) if isinstance(resp, dict) else -1


_PIPELINE_STAGES = frozenset({"execute", "publish", "review"})
_STATUS_WORD = re.compile(r"[A-Z_]{1,32}|error")
_TASK_ID = re.compile(r"task-[0-9a-f]{12}")
_HEAD_SHA = re.compile(r"[0-9a-f]{40}")
# GitHub logins: alphanumerics and single hyphens, max 39; Apps end in [bot].
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")
_NEEDS_INPUT_NEXT = (
    "Its plan is waiting for answers: reply with "
    "`/agent-platform answer <answer>` to plan again with them."
)
_REJECT_NEXT = {
    "redo": (
        "`redo` starts a new revision after a finished (COMPLETED or FAILED) "
        "task; approve the current plan with `/agent-platform approve`, or "
        "wait for its run to finish."
    ),
    "answer": (
        "`answer` replies to a plan that asked for input; use "
        "`/agent-platform redo` to plan again after a finished run."
    ),
    "approve": (
        "There is no plan to approve; comment `/agent-platform run` "
        "(or `redo` after a finished run) first."
    ),
}
_ACK_NEXT = {
    "run": "Planning now; the plan will be posted here.",
    "redo": "Planning a new revision; the plan will be posted here.",
    "answer": "Planning again with the answer; the plan will be posted here.",
    "approve": (
        "Execute, the Draft PR and the review now run automatically; "
        "a stop is reported here."
    ),
}


def _render_check_output(*, review: ReviewResult, plan_hash: str) -> str:
    """Fixed-format markdown from ReviewResult fields only."""
    lines = [f"plan: `{plan_hash}`", ""]
    if review.findings:
        lines += ["| severity | path | message |", "|---|---|---|"]
        for f in review.findings:
            lines.append(f"| {f.severity} | `{f.path}` | {f.message[:120]} |")
    else:
        lines.append("No findings — review clean.")
    return "\n".join(lines)[:_MAX_BODY_CHARS]


def _render_review_comment(
    *, review: ReviewResult, plan_hash: str, head_sha: str, marker: str
) -> str:
    """Fixed-format markdown from ReviewResult fields only. The reviewer's
    text is model output over untrusted input: `@` is defused so it cannot
    mention (notify) people, and `|` / newlines cannot break the table."""
    findings = list(review.findings)
    lines = [
        marker,
        f"### agent-platform review — {len(findings)} finding(s)",
        "",
        f"- head: `{head_sha[:12]}`",
        f"- plan: `{plan_hash}`",
    ]
    summary = _defuse(review.summary).strip()
    lines += ["", summary or "No summary provided by the reviewer."]
    if findings:
        lines += ["", "| severity | location | message |", "|---|---|---|"]
        for f in findings:
            location = f"{f.path}:{f.line}" if f.line is not None else f.path
            lines.append(
                f"| {f.severity} | `{_cell(location)}` | {_cell(f.message)[:300]} |"
            )
    else:
        lines += ["", "No findings — review clean."]
    return "\n".join(lines)[:_MAX_BODY_CHARS]


def _defuse(text: str) -> str:
    return text.replace("@", "@\u200b")


def _cell(text: str) -> str:
    return _defuse(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _check_name(repository: str, pr_number: int) -> str:
    # GitHub check names are free-form; keep them stable per PR so re-reviews
    # update rather than duplicate.
    slug = re.sub(r"[^a-zA-Z0-9._-]", "-", repository)
    return f"{_CHECK_NAME}/{slug}#{pr_number}"


def _render_plan_comment(
    *, plan: PlanResult, plan_hash: str, marker: str, awaiting_human: bool = False,
    network: str = "",
) -> str:
    title = (
        "### agent-platform plan — input needed"
        if plan.requires_human_input else "### agent-platform plan"
    )
    lines = [marker, title, "", f"- plan: `{plan_hash}`"]
    if network:
        lines.append(f"- execute network: {network}")
    if plan.summary:
        lines += ["", plan.summary]
    if plan.questions:
        lines += ["", "**Questions**"]
        lines += [f"- {q}" for q in plan.questions]
    if plan.proposed_changes:
        lines += ["", "**Proposed changes**"]
        lines += [f"- `{c.path}` — {c.reason}" for c in plan.proposed_changes]
    if plan.test_commands:
        lines += ["", "**Test commands**"]
        lines += [f"- `{c}`" for c in plan.test_commands]
    footer = ""
    if awaiting_human and plan.requires_human_input:
        footer = (
            "\n\nThis plan cannot be approved yet. Reply with "
            "`/agent-platform answer <your answer>` (a collaborator with write "
            "access); the plan is then made again with your answer."
        )
    elif awaiting_human:
        footer = (
            "\n\nTo approve this plan, reply with `/agent-platform approve` "
            "(a collaborator with write access). Execute, the Draft PR and "
            "the review then run automatically."
        )
    # Truncate the plan content, never the reply instruction.
    return "\n".join(lines)[: _MAX_BODY_CHARS - len(footer)] + footer
