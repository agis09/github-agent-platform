"""GitHub webhook receiver: start tasks from GitHub Issues.

Single-purpose HTTP server (stdlib http.server — no framework, per the
architecture rules). The receiver verifies the X-Hub-Signature-256 HMAC,
deduplicates deliveries by X-GitHub-Delivery, and routes a configurable
subset of events to TaskService:

    issues.labeled         → plan (label trigger)
    issue_comment.created  → plan (comment trigger) or approve (comment
                             approval with a strict command grammar)

Security boundaries (docs/design.md):
- the approval gate is unchanged: automated paths only ever call plan() —
  nothing in the webhook can reach execute
- comment approvals require the comment author to be on the explicit
  approver allowlist (an interim boundary until approver identity is
  fully solved); non-matching approvals are recorded as rejected
- a failed signature answers 403 and the payload is never logged
- failures inside plan()/approve() do NOT turn into HTTP 5xx (GitHub
  would retry forever); they are answered 200 with a rejected action and
  the detail goes to the audit trail instead

Trigger configuration lives in <home>/webhook_config.json (gitignored path,
per-deployment):

    {
      "trigger": "none" | "label" | "comment",
      "label_name": "agent-platform",
      "command": "/agent-platform run",
      "approvers": ["login-a"]
    }
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_platform.errors import AgentPlatformError, DomainRejectedError
from agent_platform.redaction import redact_text

_SIGNATURE_HEADER = "X-Hub-Signature-256"
_DELIVERY_HEADER = "X-GitHub-Delivery"
_EVENT_HEADER = "X-GitHub-Event"
_DEFAULT_COMMAND = "/agent-platform run"
_APPROVE_COMMAND = re.compile(
    r"/agent-platform\s+approve\s+(task-[0-9a-f]{12})\s+(sha256:[0-9a-f]{64})"
)
_APPROVE_LATEST_COMMAND = re.compile(
    r"/agent-platform\s+approve\s*$", re.IGNORECASE
)


# Delivery ids are GUIDs; event names are snake_case words. Headers are
# attacker-controlled until the signature verifies, so anything else is
# logged as a placeholder, never echoed.
_DELIVERY_ID = re.compile(r"[0-9A-Za-z-]{1,64}")
_EVENT_NAME = re.compile(r"[a-z_]{1,64}")


class WebhookConfigError(DomainRejectedError):
    """webhook_config.json is missing, malformed, or invalid."""


@dataclass(frozen=True)
class WebhookConfig:
    """Per-deployment trigger configuration. comment-kick only (the label
    trigger was removed)."""

    trigger: str = "none"  # none | comment
    command: str = _DEFAULT_COMMAND
    # Optional narrowing on top of the repo-permission gate: when empty
    # (the default), any collaborator holding >= write may approve; when
    # set, only these logins (that also hold >= write) may approve.
    approvers: tuple[str, ...] = ()
    # README「処理の流れ」: an approval continues into execute -> publish
    # (Draft PR) -> review without further commands. false keeps the
    # approval-only behaviour (execute is then run by hand).
    auto_pipeline: bool = True
    # Kicker policy for run/redo: "collaborator" verifies the comment author
    # holds >= write via permission_fn (fail-closed); "allowlist" matches
    # allowed_kickers only.
    kicker_policy: str = "collaborator"
    allowed_kickers: tuple[str, ...] = ()

    @classmethod
    def from_path(cls, path: str) -> WebhookConfig:
        try:
            raw = json.loads(Path(path).read_text())
        except FileNotFoundError as exc:
            msg = f"webhook config not found: {path}"
            raise WebhookConfigError(msg) from exc
        except ValueError as exc:
            msg = f"webhook config is not valid JSON: {path}"
            raise WebhookConfigError(msg) from exc
        if not isinstance(raw, dict):
            msg = f"webhook config must be a JSON object: {path}"
            raise WebhookConfigError(msg)
        trigger = raw.get("trigger", "none")
        if trigger not in ("none", "comment"):
            msg = f"webhook trigger must be none|comment, got {trigger!r}"
            raise WebhookConfigError(msg)
        approvers = raw.get("approvers", [])
        kickers = raw.get("allowed_kickers", [])
        for name, value in (("approvers", approvers), ("allowed_kickers", kickers)):
            if not isinstance(value, list) or not all(
                isinstance(a, str) and a for a in value
            ):
                msg = f"webhook {name} must be a list of non-empty logins"
                raise WebhookConfigError(msg)
        policy = raw.get("kicker_policy", "collaborator")
        if policy not in ("collaborator", "allowlist"):
            msg = f"webhook kicker_policy must be collaborator|allowlist, got {policy!r}"
            raise WebhookConfigError(msg)
        auto_pipeline = raw.get("auto_pipeline", True)
        if not isinstance(auto_pipeline, bool):
            msg = f"webhook auto_pipeline must be true|false, got {auto_pipeline!r}"
            raise WebhookConfigError(msg)
        return cls(
            trigger=trigger,
            auto_pipeline=auto_pipeline,
            command=str(raw.get("command", _DEFAULT_COMMAND)),
            approvers=tuple(approvers),
            kicker_policy=policy,
            allowed_kickers=tuple(kickers),
        )


@dataclass(frozen=True)
class WebhookOutcome:
    """What one delivery did (audit value + HTTP answer)."""

    status: int
    action: str  # ignored | duplicate | planned | approve-rejected | approved | rejected
    detail: str = ""


def signature_ok(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time X-Hub-Signature-256 verification (sha256=<hex>)."""
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


_RUN_COMMAND = re.compile(
    r"/agent-platform\s+run\b", re.IGNORECASE
)
_REDO_COMMAND = re.compile(
    r"/agent-platform\s+redo(?:\s+(task-[0-9a-f]{12}))?\b", re.IGNORECASE
)
_ANSWER_COMMAND = re.compile(r"/agent-platform\s+answer\b", re.IGNORECASE)


def parse_run_command(comment: str) -> bool:
    """Comment starts with the run command (leading prose not allowed; the
    command must be the first token). Trailing prose = the task context
    (untrusted, travels to the plan)."""
    return _RUN_COMMAND.match(comment.strip()) is not None


def parse_redo_command(comment: str) -> tuple[str | None, str] | None:
    """`/agent-platform redo [task-id]` as the first command; the rest of
    the comment is the follow-up feedback. Returns (task_id|None, rest)."""
    match = _REDO_COMMAND.match(comment.strip())
    if match is None:
        return None
    rest = comment.strip()[match.end():].strip()
    return (match.group(1), rest)


def parse_answer_command(comment: str) -> str | None:
    """`/agent-platform answer <text>` as the first command; returns the
    answer text (may be empty — the service rejects an empty answer)."""
    match = _ANSWER_COMMAND.match(comment.strip())
    if match is None:
        return None
    return comment.strip()[match.end():].strip()


def parse_approve_command(comment: str) -> tuple[str, str] | None:
    """/agent-platform approve <task-id> <plan-hash> — strict fullmatch, no
    trailing content. Returns (task_id, plan_hash) or None."""
    match = _APPROVE_COMMAND.fullmatch(comment.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def parse_approve_latest_command(comment: str) -> bool:
    """`/agent-platform approve` with no arguments — approve the single
    AWAITING_APPROVAL task (the explicit-args grammar stays valid; this is
    the shorthand for a human who should not need to know the hash)."""
    return _APPROVE_LATEST_COMMAND.match(comment.strip()) is not None


@dataclass
class WebhookReceiver:
    """Signature + dedup + routing. plan_fn / approve_fn are injected
    callables (TaskService methods); the receiver never touches the repo
    layer directly, so unit tests need no database."""

    secret: str
    config: WebhookConfig
    plan_fn: Callable[[str, int], object]  # (repository, issue_number)
    approve_fn: Callable[[str, str, str], object]  # (task_id, plan_hash, approver)
    # resolve_approvable_fn(repository, issue_number) -> (task_id,
    # plan_hash): the commented issue's latest task when it can be
    # approved; raises (DomainRejectedError) otherwise. Required for the
    # no-args approve shorthand; None disables it.
    resolve_approvable_fn: Callable[[str, int], tuple[str, str]] | None = None
    # redo_fn(repository, issue_number, task_id|None, feedback) — new
    # revision task from a COMPLETED predecessor. None disables redo.
    redo_fn: Callable[[str, int, str | None, str], object] | None = None
    # answer_fn(repository, issue_number, comment_id, answer) — re-plan
    # with the human's answer to a plan that asked for input. None
    # disables the answer command.
    answer_fn: Callable[[str, int, int, str], object] | None = None
    # permission_fn(login, repository) -> bool: does this author hold >=
    # write on the repo? Fail-closed (False on any uncertainty). Required
    # for run/redo/approve under kicker_policy="collaborator".
    permission_fn: Callable[[str, str], bool] | None = None
    # ack_fn(repository, issue_number, command, author, task_id|None) —
    # tell the issue a command was accepted (it passed every gate). Called
    # before the slow plan of run/redo/answer, after the approval is
    # recorded for approve. The implementation decides failure handling.
    ack_fn: Callable[[str, int, str, str, str | None], None] | None = None
    # check_fn(command, repository, issue_number, task_id|None) — read-only
    # pre-check for redo / answer, run before the ack so a command the
    # task state refuses is never announced as accepted. Raises
    # DomainRejectedError; None skips the pre-check.
    check_fn: Callable[[str, str, int, str | None], None] | None = None
    # reject_fn(repository, issue_number, command, author, error) — tell the
    # issue a permitted author's redo / answer / approve was refused by a
    # domain rule. The implementation decides failure handling.
    reject_fn: Callable[[str, int, str, str, DomainRejectedError], None] | None = None
    _seen: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def handle(self, *, headers: Mapping[str, str], body: bytes) -> WebhookOutcome:
        signature = headers.get(_SIGNATURE_HEADER, "")
        delivery = headers.get(_DELIVERY_HEADER, "")
        event = headers.get(_EVENT_HEADER, "")
        if not signature_ok(self.secret, body, signature):
            # Never log or echo the payload on a signature failure.
            return WebhookOutcome(status=403, action="ignored", detail="bad signature")
        if not delivery:
            return WebhookOutcome(status=400, action="ignored", detail="missing delivery id")
        with self._lock:
            if delivery in self._seen:
                return WebhookOutcome(status=200, action="duplicate", detail=delivery)
            self._seen.add(delivery)
        try:
            payload = json.loads(body.decode())
        except (ValueError, UnicodeDecodeError):
            return WebhookOutcome(status=400, action="ignored", detail="invalid JSON")
        if not isinstance(payload, dict):
            return WebhookOutcome(status=200, action="ignored", detail="unexpected payload")

        try:
            return self._route(event, payload)
        except AgentPlatformError as exc:
            # Surface in the audit trail, not as an HTTP error (GitHub would
            # retry 5xx forever).
            return WebhookOutcome(status=200, action="rejected", detail=str(exc)[:200])

    def _route(self, event: str, payload: dict[str, object]) -> WebhookOutcome:
        if event == "issue_comment" and payload.get("action") == "created":
            return self._on_comment(payload)
        return WebhookOutcome(status=200, action="ignored", detail=f"event {event or 'unknown'}")

    def _kicker_ok(self, author: str, repository: str) -> bool:
        cfg = self.config
        if author == "" or author == "[bot]":
            return False
        if cfg.kicker_policy == "allowlist":
            return author in cfg.allowed_kickers
        # collaborator policy: verified via permission_fn (fail-closed —
        # a missing or failing checker denies every kick).
        if self.permission_fn is None:
            return False
        return bool(self.permission_fn(author, repository))

    def _on_comment(self, payload: dict[str, object]) -> WebhookOutcome:
        cfg = self.config
        if cfg.trigger != "comment":
            return WebhookOutcome(status=200, action="ignored", detail="trigger disabled")
        comment = str(_sub_dict(payload, "comment").get("body", ""))
        author = str(_sub_dict(_sub_dict(payload, "comment"), "user").get("login", ""))
        target = _issue_ref(payload)
        if target is None:
            return WebhookOutcome(status=200, action="ignored", detail="malformed payload")
        repository, issue_number = target

        # 1. run: new plan (first task for this issue).
        if parse_run_command(comment):
            if not self._kicker_ok(author, repository):
                return WebhookOutcome(
                    status=200, action="kick-rejected",
                    detail=f"author {author!r} may not kick",
                )
            self._ack(repository, issue_number, "run", author)
            self.plan_fn(repository, issue_number)
            planned = f"{repository}#{issue_number}"
            return WebhookOutcome(status=200, action="planned", detail=planned)

        # 2. redo: new revision from a COMPLETED or FAILED task.
        redo = parse_redo_command(comment)
        if redo is not None:
            if self.redo_fn is None:
                return WebhookOutcome(status=200, action="ignored", detail="redo not available")
            if not self._kicker_ok(author, repository):
                return WebhookOutcome(
                    status=200, action="kick-rejected",
                    detail=f"author {author!r} may not kick",
                )
            task_id, feedback = redo
            redo_fn = self.redo_fn
            with self._reporting_rejection(repository, issue_number, "redo", author):
                self._check("redo", repository, issue_number, task_id)
                self._ack(repository, issue_number, "redo", author)
                redo_fn(repository, issue_number, task_id, feedback)
            ref = f"{repository}#{issue_number}"
            return WebhookOutcome(status=200, action="redo-planned", detail=ref)

        # 3. answer: reply to a plan that asked for human input.
        answer = parse_answer_command(comment)
        if answer is not None:
            if self.answer_fn is None:
                return WebhookOutcome(status=200, action="ignored", detail="answer not available")
            if not self._kicker_ok(author, repository):
                return WebhookOutcome(
                    status=200, action="kick-rejected",
                    detail=f"author {author!r} may not answer",
                )
            comment_id = _sub_dict(payload, "comment").get("id")
            if not isinstance(comment_id, int) or comment_id < 1:
                return WebhookOutcome(status=200, action="ignored", detail="malformed payload")
            answer_fn = self.answer_fn
            with self._reporting_rejection(repository, issue_number, "answer", author):
                self._check("answer", repository, issue_number, None)
                self._ack(repository, issue_number, "answer", author)
                answer_fn(repository, issue_number, comment_id, answer)
            ref = f"{repository}#{issue_number}"
            return WebhookOutcome(status=200, action="answer-planned", detail=ref)

        # 4. approve: explicit-args grammar or the no-args shorthand, then
        # the repo-permission gate. The author must hold >= write on the
        # repository, verified via permission_fn (the same fail-closed
        # check as the kicker gate); a non-empty allowlist further narrows
        # who may approve.
        parsed = parse_approve_command(comment)
        latest = parse_approve_latest_command(comment)
        if parsed is None and not latest:
            return WebhookOutcome(status=200, action="ignored", detail="not a command")
        if not self._kicker_ok(author, repository):
            return WebhookOutcome(
                status=200, action="approve-rejected",
                detail=f"author {author!r} may not approve",
            )
        if cfg.approvers and author not in cfg.approvers:
            return WebhookOutcome(
                status=200, action="approve-rejected",
                detail=f"author {author!r} is not an allowed approver",
            )
        if latest and self.resolve_approvable_fn is None:
            return WebhookOutcome(
                status=200, action="ignored", detail="shorthand not available",
            )
        with self._reporting_rejection(repository, issue_number, "approve", author):
            if latest and self.resolve_approvable_fn is not None:
                # Resolve this issue's latest task — the human never needs
                # the task id or hash. Nothing approvable raises
                # (-> "rejected").
                task_id, plan_hash = self.resolve_approvable_fn(
                    repository, issue_number
                )
            elif parsed is not None:
                task_id, plan_hash = parsed
            else:
                # Unreachable: not latest means parsed was not None above.
                return WebhookOutcome(
                    status=200, action="ignored", detail="not a command"
                )
            self.approve_fn(task_id, plan_hash, author)
        self._ack(repository, issue_number, "approve", author, task_id)
        return WebhookOutcome(status=200, action="approved", detail=task_id)

    def _ack(self, repository: str, issue_number: int, command: str,
             author: str, task_id: str | None = None) -> None:
        if self.ack_fn is not None:
            self.ack_fn(repository, issue_number, command, author, task_id)

    def _check(self, command: str, repository: str, issue_number: int,
               task_id: str | None) -> None:
        if self.check_fn is not None:
            self.check_fn(command, repository, issue_number, task_id)

    @contextmanager
    def _reporting_rejection(
        self, repository: str, issue_number: int, command: str, author: str
    ) -> Iterator[None]:
        """Report a domain-rule refusal on the issue, then let it propagate
        (-> "rejected" outcome). Only reached after the permission gate, so
        unpermitted authors never make the bot reply."""
        try:
            yield
        except DomainRejectedError as exc:
            if self.reject_fn is not None:
                self.reject_fn(repository, issue_number, command, author, exc)
            raise


def format_delivery_log(headers: Mapping[str, str], outcome: WebhookOutcome) -> str:
    """One audit line per delivery: delivery id + event + HTTP status +
    action. The payload, the signature and the outcome detail (which may
    carry author logins or exception text) are never included."""
    delivery = headers.get(_DELIVERY_HEADER, "")
    event = headers.get(_EVENT_HEADER, "")
    if not _DELIVERY_ID.fullmatch(delivery):
        delivery = "<invalid>"
    if not _EVENT_NAME.fullmatch(event):
        event = "<invalid>"
    return (
        f"webhook: delivery={delivery} event={event} "
        f"status={outcome.status} action={outcome.action}"
    )


def _sub_dict(payload: dict[str, object], key: str) -> dict[str, object]:
    """Read a nested JSON object, tolerating absence and non-dict values."""
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _issue_ref(payload: dict[str, object]) -> tuple[str, int] | None:
    repo = _sub_dict(payload, "repository")
    issue = _sub_dict(payload, "issue")
    repo_full = str(repo.get("full_name", ""))
    number = issue.get("number")
    if not repo_full or not isinstance(number, int) or number < 1:
        return None
    return repo_full, number


def make_handler(receiver: WebhookReceiver) -> type[BaseHTTPRequestHandler]:
    """Bind a receiver to a stdlib request-handler class."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — stdlib naming
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            header_map: dict[str, str] = {
                k: self.headers.get(k, "") for k in (
                    _SIGNATURE_HEADER, _DELIVERY_HEADER, _EVENT_HEADER,
                )
            }
            outcome = receiver.handle(headers=header_map, body=body)
            # Receipt record for "the delivery never arrived" triage;
            # stderr goes to the pod log.
            print(format_delivery_log(header_map, outcome), file=sys.stderr, flush=True)
            if outcome.action == "rejected":
                # The refusal reason (our own exception text: task ids,
                # states), redacted as defence in depth.
                print(f"webhook: rejected: {redact_text(outcome.detail)}",
                      file=sys.stderr, flush=True)
            self.send_response(outcome.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"action": outcome.action}).encode())

        def do_GET(self) -> None:  # noqa: N802 — stdlib naming
            # Liveness probe only (k8s-placed receiver); anything but
            # /healthz is a 404. No request content is echoed back.
            if self.path == "/healthz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            # Minimal path-only logging; payloads never reach the log.
            pass

    return Handler


def serve(receiver: WebhookReceiver, *, port: int, bind: str = "127.0.0.1") -> None:
    """Run the webhook server until interrupted (agentctl webhook serve).

    Default bind stays 127.0.0.1 (local use); a cloud-placed receiver
    (k8s Orchestrator pod) passes 0.0.0.0 explicitly."""
    server = ThreadingHTTPServer((bind, port), make_handler(receiver))
    print(f"webhook listening on {bind}:{port} (trigger={receiver.config.trigger})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
