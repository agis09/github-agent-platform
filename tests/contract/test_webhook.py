"""Contract: webhook receiver — comment-kick design: signature verification,
delivery dedup, run/redo/approve grammar, and the collaborator kicker gate
(fail-closed)."""

import hashlib
import hmac
import json

import pytest

from agent_platform.errors import (
    DomainRejectedError,
    StateConflictError,
    TaskStateRejectedError,
)
from agent_platform.webhook import (
    WebhookConfig,
    WebhookConfigError,
    WebhookReceiver,
    parse_approve_command,
    parse_redo_command,
    parse_run_command,
    serve,
    signature_ok,
)

SECRET = "whsec-test"
PLAN_HASH = "sha256:" + "a" * 64
TASK_ID = "task-0123456789ab"


_DELIVERY_COUNTER = [0]


def _signed_body(payload: dict, event: str = "issue_comment",
                 secret: str = SECRET, delivery: str | None = None) -> tuple[dict, bytes]:
    if delivery is None:
        _DELIVERY_COUNTER[0] += 1
        delivery = f"delivery-{_DELIVERY_COUNTER[0]}"
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": event,
    }
    return headers, body


def _comment_payload(body: str, author: str = "alice") -> dict:
    return {
        "action": "created",
        "issue": {"number": 1},
        "repository": {"full_name": "owner/repo"},
        "comment": {"body": body, "user": {"login": author}},
    }


def _receiver(**overrides) -> tuple[WebhookReceiver, dict]:
    planned: list[tuple[str, int]] = []
    approved: list[tuple[str, str, str]] = []
    redone: list[tuple[str, int, str | None, str]] = []
    cfg = WebhookConfig(
        trigger=overrides.get("trigger", "comment"),
        command=overrides.get("command", "/agent-platform run"),
        approvers=tuple(overrides.get("approvers", ())),
        kicker_policy=overrides.get("kicker_policy", "collaborator"),
        allowed_kickers=tuple(overrides.get("allowed_kickers", ())),
    )

    def plan_fn(repo, number):
        planned.append((repo, number))

    def approve_fn(task_id, plan_hash, approver):
        approved.append((task_id, plan_hash, approver))

    def redo_fn(repo, number, task_id, feedback):
        redone.append((repo, number, task_id, feedback))

    def permission_fn(login, repo):
        return overrides.get("permitted", True)

    receiver = WebhookReceiver(
        secret=SECRET, config=cfg, plan_fn=plan_fn, approve_fn=approve_fn,
        redo_fn=redo_fn, permission_fn=permission_fn,
    )
    return receiver, {"planned": planned, "approved": approved, "redone": redone}


# --- signature ---

def test_signature_ok_valid_and_invalid():
    body = b'{"x": 1}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert signature_ok(SECRET, body, good)
    assert not signature_ok(SECRET, body, "sha256=" + "0" * 64)
    assert not signature_ok(SECRET, body, "sha1=deadbeef")


def test_bad_signature_is_403_and_payload_not_processed():
    receiver, rec = _receiver()
    outcome = receiver.handle(
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64,
                 "X-GitHub-Delivery": "d1", "X-GitHub-Event": "issue_comment"},
        body=json.dumps(_comment_payload("/agent-platform run")).encode(),
    )
    assert outcome.status == 403
    assert rec["planned"] == []


def test_delivery_dedup_same_id_is_duplicate():
    receiver, rec = _receiver()
    headers, body = _signed_body(_comment_payload("/agent-platform run"))
    first = receiver.handle(headers=headers, body=body)
    second = receiver.handle(headers=headers, body=body)
    assert first.action == "planned"
    assert second.action == "duplicate"
    assert rec["planned"] == [("owner/repo", 1)]


# --- run command ---

def test_run_command_plans_when_kicker_permitted():
    receiver, rec = _receiver(permitted=True)
    h, b = _signed_body(_comment_payload("/agent-platform run"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "planned"
    assert rec["planned"] == [("owner/repo", 1)]


def test_run_command_rejected_when_kicker_not_permitted():
    receiver, rec = _receiver(permitted=False)
    h, b = _signed_body(_comment_payload("/agent-platform run"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "kick-rejected"
    assert rec["planned"] == []


def test_run_command_case_insensitive_with_trailing_context():
    receiver, rec = _receiver()
    h, b = _signed_body(_comment_payload("/Agent-Platform Run — score resets after game over"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "planned"


def test_run_command_rejected_when_permission_fn_missing():
    """Fail-closed: no permission checker configured → nobody kicks."""
    receiver, rec = _receiver()
    receiver.permission_fn = None
    h, b = _signed_body(_comment_payload("/agent-platform run"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "kick-rejected"


def test_allowlist_policy_ignores_permission_fn():
    receiver, rec = _receiver(kicker_policy="allowlist", allowed_kickers=("bob",))
    h, b = _signed_body(_comment_payload("/agent-platform run", author="bob"))
    assert receiver.handle(headers=h, body=b).action == "planned"
    h, b = _signed_body(_comment_payload("/agent-platform run", author="carol"))
    assert receiver.handle(headers=h, body=b).action == "kick-rejected"


def test_trigger_none_ignores_everything():
    receiver, rec = _receiver(trigger="none")
    h, b = _signed_body(_comment_payload("/agent-platform run"))
    assert receiver.handle(headers=h, body=b).action == "ignored"
    assert rec["planned"] == []


# --- redo command ---

def test_redo_with_task_id_and_feedback():
    receiver, rec = _receiver()
    h, b = _signed_body(_comment_payload(
        f"/agent-platform redo {TASK_ID} — score breaks when invaders wrap"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "redo-planned"
    assert rec["redone"] == [("owner/repo", 1, TASK_ID,
                              "— score breaks when invaders wrap")]


def test_redo_without_task_id_passes_none():
    receiver, rec = _receiver()
    h, b = _signed_body(_comment_payload("/agent-platform redo"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "redo-planned"
    assert rec["redone"] == [("owner/repo", 1, None, "")]


def test_redo_requires_kicker_permission():
    receiver, rec = _receiver(permitted=False)
    h, b = _signed_body(_comment_payload("/agent-platform redo"))
    assert receiver.handle(headers=h, body=b).action == "kick-rejected"
    assert rec["redone"] == []


# --- approve ---

def test_comment_approval_with_allowed_approver():
    receiver, rec = _receiver(approvers=("alice",), permitted=True)
    command = f"/agent-platform approve {TASK_ID} {PLAN_HASH}"
    h, b = _signed_body(_comment_payload(command))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "approved"
    assert rec["approved"] == [(TASK_ID, PLAN_HASH, "alice")]


def test_comment_approval_with_empty_allowlist_allows_permitted_collaborator():
    # Repo-permission gate only (the deploy default): any collaborator
    # holding >= write may approve — the allowlist narrows, never widens.
    receiver, rec = _receiver(approvers=(), permitted=True)
    command = f"/agent-platform approve {TASK_ID} {PLAN_HASH}"
    h, b = _signed_body(_comment_payload(command))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "approved"
    assert rec["approved"] == [(TASK_ID, PLAN_HASH, "alice")]


def test_comment_approval_with_empty_allowlist_rejects_unpermitted_author():
    # Fail-closed: without >= write, the empty allowlist approves nobody.
    receiver, rec = _receiver(approvers=(), permitted=False)
    command = f"/agent-platform approve {TASK_ID} {PLAN_HASH}"
    h, b = _signed_body(_comment_payload(command))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "approve-rejected"
    assert rec["approved"] == []


def test_allowlist_narrows_permission_gate():
    # A permitted collaborator outside the allowlist is still rejected.
    receiver, rec = _receiver(approvers=("bob",), permitted=True)
    command = f"/agent-platform approve {TASK_ID} {PLAN_HASH}"
    h, b = _signed_body(_comment_payload(command, author="alice"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "approve-rejected"
    assert rec["approved"] == []


# --- no-args approve shorthand (scoped to the commented issue) ---


def _receiver_with_resolver(resolver, **overrides):
    receiver, rec = _receiver(**overrides)
    calls: list[tuple[str, int]] = []

    def resolve(repo, number):
        calls.append((repo, number))
        return resolver(repo, number)

    receiver.resolve_approvable_fn = resolve
    rec["resolved"] = calls
    return receiver, rec


def test_approve_shorthand_resolves_the_commented_issue():
    receiver, rec = _receiver_with_resolver(lambda r, n: (TASK_ID, PLAN_HASH), permitted=True)
    h, b = _signed_body(_comment_payload("/agent-platform approve"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "approved"
    assert rec["resolved"] == [("owner/repo", 1)]
    assert rec["approved"] == [(TASK_ID, PLAN_HASH, "alice")]


def test_approve_shorthand_rejected_when_issue_has_nothing_to_approve():
    def resolver(repo, number):
        raise StateConflictError("latest task task-aaaaaaaaaaaa is FAILED, nothing to approve")

    receiver, rec = _receiver_with_resolver(resolver, permitted=True)
    h, b = _signed_body(_comment_payload("/agent-platform approve"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.status == 200
    assert outcome.action == "rejected"
    assert "nothing to approve" in outcome.detail
    assert rec["approved"] == []


def test_approve_shorthand_requires_kicker_permission():
    receiver, rec = _receiver_with_resolver(lambda r, n: (TASK_ID, PLAN_HASH), permitted=False)
    h, b = _signed_body(_comment_payload("/agent-platform approve"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "approve-rejected"
    assert rec["approved"] == []
    # Gate order: an unpermitted author learns nothing — no resolution ran.
    assert rec["resolved"] == []


def test_approve_shorthand_ignored_when_resolver_missing():
    receiver, rec = _receiver(permitted=True)  # resolve_approvable_fn stays None
    h, b = _signed_body(_comment_payload("/agent-platform approve"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "ignored"
    assert rec["approved"] == []


def test_comment_approval_from_non_approver_is_rejected():
    receiver, rec = _receiver(approvers=("alice",))
    command = f"/agent-platform approve {TASK_ID} {PLAN_HASH}"
    h, b = _signed_body(_comment_payload(command, author="mallory"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "approve-rejected"
    assert rec["approved"] == []


def test_approve_also_requires_kicker_permission():
    receiver, rec = _receiver(approvers=("alice",), permitted=False)
    command = f"/agent-platform approve {TASK_ID} {PLAN_HASH}"
    h, b = _signed_body(_comment_payload(command))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "approve-rejected"
    assert rec["approved"] == []


def test_approve_command_requires_strict_grammar():
    assert parse_approve_command(f"/agent-platform approve {TASK_ID} {PLAN_HASH}") == (
        TASK_ID, PLAN_HASH)
    assert parse_approve_command(f"/agent-platform approve {TASK_ID} {PLAN_HASH} extra") is None
    assert parse_approve_command(f"/agent-platform approve {TASK_ID} deadbeef") is None


# --- grammar edge cases ---

def test_run_grammar_matches_prefix_only():
    assert parse_run_command("/agent-platform run")
    assert parse_run_command("/agent-platform run extra context")
    assert not parse_run_command("please /agent-platform run")
    assert not parse_run_command("/agent-platform runner-up")
    assert not parse_run_command("/agent-platform redo")


def test_redo_grammar_extracts_rest():
    assert parse_redo_command("/agent-platform redo") == (None, "")
    assert parse_redo_command("/agent-platform redo task-0123456789ab x") == (
        "task-0123456789ab", "x")
    assert parse_redo_command("later /agent-platform redo") is None


# --- failure handling ---

def test_plan_failure_surfaces_as_rejected_not_http_error():
    """A failing plan_fn must not raise out of handle() (GitHub would retry
    5xx forever); it becomes a rejected action with the detail."""
    def failing_plan(repo, number):
        raise DomainRejectedError("plan proposed no file changes")

    cfg = WebhookConfig(trigger="comment")
    receiver = WebhookReceiver(
        secret=SECRET, config=cfg, plan_fn=failing_plan,
        approve_fn=lambda *a: None, redo_fn=lambda *a: None,
        permission_fn=lambda *a: True,
    )
    h, b = _signed_body(_comment_payload("/agent-platform run"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.status == 200
    assert outcome.action == "rejected"
    assert "proposed no file changes" in outcome.detail


# --- config ---

def test_config_rejects_unknown_trigger(tmp_path):
    cfg = tmp_path / "webhook_config.json"
    cfg.write_text(json.dumps({"trigger": "label"}))
    with pytest.raises(WebhookConfigError):
        WebhookConfig.from_path(str(cfg))


def test_config_rejects_bad_kicker_policy(tmp_path):
    cfg = tmp_path / "webhook_config.json"
    cfg.write_text(json.dumps({"kicker_policy": "magic"}))
    with pytest.raises(WebhookConfigError):
        WebhookConfig.from_path(str(cfg))


def test_config_missing_file_raises(tmp_path):
    with pytest.raises(WebhookConfigError):
        WebhookConfig.from_path(str(tmp_path / "missing.json"))



# --- serve() bind address (webhook cloud placement) ---

def test_serve_binds_configured_address(monkeypatch):
    """serve() must bind the passed address, not hardcode 127.0.0.1. The
    cloud-placed receiver (k8s Orchestrator pod) needs a non-loopback bind."""
    import agent_platform.webhook as wh

    captured: dict = {}

    class FakeServer:
        def __init__(self, addr, handler):
            captured["addr"] = addr
            captured["handler"] = handler

        def serve_forever(self):
            raise KeyboardInterrupt  # exit serve() immediately

        def server_close(self):
            captured["closed"] = True

    monkeypatch.setattr(wh, "ThreadingHTTPServer", FakeServer)
    cfg = WebhookConfig(trigger="none")
    receiver = WebhookReceiver(
        secret=SECRET, config=cfg, plan_fn=lambda *a: None,
        approve_fn=lambda *a: None, redo_fn=None, permission_fn=None,
    )
    serve(receiver, port=8080, bind="0.0.0.0")
    assert captured["addr"] == ("0.0.0.0", 8080)
    assert captured["closed"] is True


# --- healthz (liveness probe for the k8s-placed receiver) ---

def test_healthz_get_returns_ok():
    """GET /healthz is the liveness probe path; returns 200 JSON."""
    import io

    from agent_platform.webhook import make_handler

    cfg = WebhookConfig(trigger="none")
    receiver = WebhookReceiver(
        secret=SECRET, config=cfg, plan_fn=lambda *a: None,
        approve_fn=lambda *a: None, redo_fn=None, permission_fn=None,
    )
    handler_cls = make_handler(receiver)
    handler = handler_cls.__new__(handler_cls)  # bypass __init__'s socket wiring
    handler.path = "/healthz"
    out = io.BytesIO()


    def _send_response(code):
        handler._headers_buffer = []  # type: ignore[attr-defined]
        out.write(b"HTTP/1.1 %d\r\n" % code)


    def _send_header(k, v):
        out.write(b"%s: %s\r\n" % (k.encode(), v.encode()))


    def _end_headers():
        out.write(b"\r\n")


    handler.send_response = _send_response  # type: ignore[method-assign]
    handler.send_header = _send_header  # type: ignore[method-assign]
    handler.end_headers = _end_headers  # type: ignore[method-assign]
    handler.wfile = out  # type: ignore[method-assign]
    handler.do_GET()
    body = out.getvalue()
    assert b"HTTP/1.1 200" in body
    assert b'"status": "ok"' in body


def test_unknown_get_path_is_404():
    import io

    from agent_platform.webhook import make_handler

    cfg = WebhookConfig(trigger="none")
    receiver = WebhookReceiver(
        secret=SECRET, config=cfg, plan_fn=lambda *a: None,
        approve_fn=lambda *a: None, redo_fn=None, permission_fn=None,
    )
    handler_cls = make_handler(receiver)
    handler = handler_cls.__new__(handler_cls)
    handler.path = "/payloads"
    out = io.BytesIO()


    def _send_response(code):
        out.write(b"HTTP/1.1 %d\r\n" % code)


    handler.send_response = _send_response  # type: ignore[method-assign]
    handler.send_header = lambda k, v: None  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]
    handler.wfile = out  # type: ignore[method-assign]
    handler.do_GET()
    assert b"HTTP/1.1 404" in out.getvalue()


# --- delivery receipt log ---


def test_delivery_log_has_id_event_status_action_only():
    from agent_platform.webhook import WebhookOutcome, format_delivery_log

    line = format_delivery_log(
        {
            "X-GitHub-Delivery": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
            "X-GitHub-Event": "issue_comment",
            "X-Hub-Signature-256": "sha256=" + "a" * 64,
        },
        WebhookOutcome(status=200, action="kick-rejected", detail="author 'mallory' may not kick"),
    )
    assert line == (
        "webhook: delivery=72d3162e-cc78-11e3-81ab-4c9367dc0958 "
        "event=issue_comment status=200 action=kick-rejected"
    )
    # Neither the signature nor the outcome detail reaches the log.
    assert "sha256=" not in line
    assert "mallory" not in line


def test_delivery_log_replaces_unexpected_header_values():
    from agent_platform.webhook import WebhookOutcome, format_delivery_log

    line = format_delivery_log(
        {"X-GitHub-Delivery": "x\nwebhook: forged line", "X-GitHub-Event": "a b"},
        WebhookOutcome(status=403, action="ignored", detail="bad signature"),
    )
    assert line == (
        "webhook: delivery=<invalid> event=<invalid> status=403 action=ignored"
    )
    assert "\n" not in line


def test_handler_writes_one_receipt_line_per_delivery(capsys):
    import io

    from agent_platform.webhook import WebhookConfig, WebhookReceiver, make_handler

    receiver = WebhookReceiver(
        secret="s3cr3t-canary", config=WebhookConfig(),
        plan_fn=lambda *a: None, approve_fn=lambda *a: None,
    )
    handler_cls = make_handler(receiver)
    body = b'{"canary-payload": true}'

    class _FakeHandler(handler_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            self.headers = {
                "Content-Length": str(len(body)),
                "X-GitHub-Delivery": "d-1",
                "X-GitHub-Event": "issue_comment",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
            }
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()

        def send_response(self, code, message=None):
            self.code = code

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

    _FakeHandler().do_POST()
    err = capsys.readouterr().err
    assert err == "webhook: delivery=d-1 event=issue_comment status=403 action=ignored\n"
    assert "canary-payload" not in err


# --- auto_pipeline config ---


def test_config_auto_pipeline_defaults_on(tmp_path):
    cfg = tmp_path / "webhook_config.json"
    cfg.write_text(json.dumps({"trigger": "comment"}))
    assert WebhookConfig.from_path(str(cfg)).auto_pipeline is True


def test_config_auto_pipeline_can_be_disabled(tmp_path):
    cfg = tmp_path / "webhook_config.json"
    cfg.write_text(json.dumps({"trigger": "comment", "auto_pipeline": False}))
    assert WebhookConfig.from_path(str(cfg)).auto_pipeline is False


def test_config_auto_pipeline_rejects_non_bool(tmp_path):
    cfg = tmp_path / "webhook_config.json"
    cfg.write_text(json.dumps({"auto_pipeline": "yes"}))
    with pytest.raises(WebhookConfigError, match="auto_pipeline"):
        WebhookConfig.from_path(str(cfg))


# --- answer command (reply to a plan that asked for input) ---


def _answer_payload(body, author="alice", comment_id=4242):
    payload = _comment_payload(body, author=author)
    payload["comment"]["id"] = comment_id
    return payload


def _receiver_with_answer(**overrides):
    receiver, rec = _receiver(**overrides)
    answered: list[tuple[str, int, int, str]] = []
    receiver.answer_fn = lambda repo, n, cid, text: answered.append((repo, n, cid, text))
    rec["answered"] = answered
    return receiver, rec


def test_answer_routes_text_and_comment_id():
    receiver, rec = _receiver_with_answer(permitted=True)
    h, b = _signed_body(_answer_payload("/agent-platform answer\nmax_tokens は 1024 固定"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "answer-planned"
    assert rec["answered"] == [("owner/repo", 1, 4242, "max_tokens は 1024 固定")]
    assert rec["approved"] == [] and rec["planned"] == []


def test_answer_requires_write_permission():
    receiver, rec = _receiver_with_answer(permitted=False)
    h, b = _signed_body(_answer_payload("/agent-platform answer 1024"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "kick-rejected"
    assert rec["answered"] == []


def test_answer_without_comment_id_is_ignored():
    receiver, rec = _receiver_with_answer(permitted=True)
    h, b = _signed_body(_comment_payload("/agent-platform answer 1024"))
    outcome = receiver.handle(headers=h, body=b)
    assert outcome.action == "ignored"
    assert rec["answered"] == []


def test_answer_ignored_when_not_wired():
    receiver, rec = _receiver(permitted=True)
    h, b = _signed_body(_answer_payload("/agent-platform answer 1024"))
    assert receiver.handle(headers=h, body=b).action == "ignored"


def test_answer_is_not_an_approval():
    # "answer" must never fall through to the approve grammar.
    receiver, rec = _receiver_with_answer(permitted=True)
    h, b = _signed_body(_answer_payload("/agent-platform answer approve"))
    receiver.handle(headers=h, body=b)
    assert rec["approved"] == []
    assert rec["answered"][0][3] == "approve"


# --- command acknowledgement (the issue sees the command arrived) ---


def _receiver_with_ack(resolver=None, **overrides):
    """Receiver whose ack and command calls go into one ordered log."""
    receiver, rec = _receiver_with_answer(**overrides)
    events: list[tuple] = []
    receiver.plan_fn = lambda repo, n: events.append(("plan", repo, n))
    receiver.redo_fn = lambda repo, n, task_id, fb: events.append(("redo", repo, n))
    receiver.answer_fn = lambda repo, n, cid, text: events.append(("answer", repo, n))
    receiver.approve_fn = lambda task_id, h, who: events.append(("approve", task_id, who))
    receiver.resolve_approvable_fn = resolver or (lambda r, n: (TASK_ID, PLAN_HASH))
    receiver.ack_fn = lambda repo, n, cmd, who, task_id: events.append(
        ("ack", repo, n, cmd, who, task_id)
    )
    return receiver, events


@pytest.mark.parametrize(
    ("comment", "command", "action"),
    [
        ("/agent-platform run", "run", "plan"),
        ("/agent-platform redo", "redo", "redo"),
        ("/agent-platform answer 1024", "answer", "answer"),
    ],
)
def test_planning_commands_are_acknowledged_before_planning(comment, command, action):
    receiver, events = _receiver_with_ack(permitted=True)
    h, b = _signed_body(_answer_payload(comment))
    receiver.handle(headers=h, body=b)
    assert events == [
        ("ack", "owner/repo", 1, command, "alice", None),
        (action, "owner/repo", 1),
    ]


@pytest.mark.parametrize(
    "comment", ["/agent-platform approve", f"/agent-platform approve {TASK_ID} {PLAN_HASH}"],
)
def test_approve_is_acknowledged_after_the_approval_is_recorded(comment):
    receiver, events = _receiver_with_ack(permitted=True)
    h, b = _signed_body(_comment_payload(comment))
    assert receiver.handle(headers=h, body=b).action == "approved"
    assert events == [
        ("approve", TASK_ID, "alice"),
        ("ack", "owner/repo", 1, "approve", "alice", TASK_ID),
    ]


@pytest.mark.parametrize(
    "comment",
    ["/agent-platform run", "/agent-platform redo", "/agent-platform answer x",
     "/agent-platform approve"],
)
def test_rejected_commands_are_not_acknowledged(comment):
    receiver, events = _receiver_with_ack(permitted=False)
    h, b = _signed_body(_answer_payload(comment))
    assert receiver.handle(headers=h, body=b).action in ("kick-rejected", "approve-rejected")
    assert events == []


def test_approve_with_nothing_to_approve_is_not_acknowledged():
    def resolver(repo, number):
        raise StateConflictError("nothing to approve")

    receiver, events = _receiver_with_ack(resolver=resolver, permitted=True)
    h, b = _signed_body(_comment_payload("/agent-platform approve"))
    assert receiver.handle(headers=h, body=b).action == "rejected"
    assert events == []


def test_plain_comments_are_not_acknowledged():
    receiver, events = _receiver_with_ack(permitted=True)
    h, b = _signed_body(_comment_payload("thanks, looks good"))
    assert receiver.handle(headers=h, body=b).action == "ignored"
    assert events == []


# --- refused commands: pre-check before the ack, notice on the issue ---


def _receiver_with_reject(check=None, **overrides):
    receiver, events = _receiver_with_ack(**overrides)
    receiver.check_fn = check or (lambda cmd, repo, n, task_id: None)
    receiver.reject_fn = lambda repo, n, cmd, who, exc: events.append(
        ("reject", repo, n, cmd, who, type(exc).__name__)
    )
    return receiver, events


@pytest.mark.parametrize(
    ("comment", "command"),
    [("/agent-platform redo", "redo"), ("/agent-platform answer 1024", "answer")],
)
def test_refused_planning_command_is_reported_not_acknowledged(comment, command):
    """A redo the task state refuses used to be acknowledged as
    accepted, then rejected silently."""
    def check(cmd, repo, n, task_id):
        raise TaskStateRejectedError(
            "not now", task_id=TASK_ID, status="AWAITING_APPROVAL", needs_input=True,
        )

    receiver, events = _receiver_with_reject(check=check, permitted=True)
    h, b = _signed_body(_answer_payload(comment))
    assert receiver.handle(headers=h, body=b).action == "rejected"
    assert events == [
        ("reject", "owner/repo", 1, command, "alice", "TaskStateRejectedError"),
    ]


def test_precheck_runs_before_ack_and_command():
    calls = []

    def check(cmd, repo, n, task_id):
        calls.append(("check", cmd, task_id))

    receiver, events = _receiver_with_reject(check=check, permitted=True)
    h, b = _signed_body(_answer_payload(f"/agent-platform redo {TASK_ID}"))
    assert receiver.handle(headers=h, body=b).action == "redo-planned"
    assert calls == [("check", "redo", TASK_ID)]
    assert [e[0] for e in events] == ["ack", "redo"]


def test_refusal_inside_the_command_is_reported():
    """A state change between pre-check and plan still surfaces on the issue."""
    receiver, events = _receiver_with_reject(permitted=True)

    def redo(repo, n, task_id, fb):
        raise StateConflictError("concurrent")

    receiver.redo_fn = redo
    h, b = _signed_body(_answer_payload("/agent-platform redo"))
    assert receiver.handle(headers=h, body=b).action == "rejected"
    assert [e[0] for e in events] == ["ack", "reject"]


def test_refused_approve_is_reported():
    def resolver(repo, number):
        raise StateConflictError("nothing to approve")

    receiver, events = _receiver_with_reject(resolver=resolver, permitted=True)
    h, b = _signed_body(_comment_payload("/agent-platform approve"))
    assert receiver.handle(headers=h, body=b).action == "rejected"
    assert events == [
        ("reject", "owner/repo", 1, "approve", "alice", "StateConflictError"),
    ]


@pytest.mark.parametrize(
    "comment", ["/agent-platform redo", "/agent-platform answer x", "/agent-platform approve"],
)
def test_unpermitted_authors_get_no_rejection_notice(comment):
    receiver, events = _receiver_with_reject(permitted=False)
    h, b = _signed_body(_answer_payload(comment))
    receiver.handle(headers=h, body=b)
    assert events == []
