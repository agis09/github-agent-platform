"""Contract: GitHub reporter — schema-backed content only, idempotent
comment/check posting. Uses an in-memory fake client (no network)."""


from agent_platform.domain.models import (
    CommandRecord,
    ExecutionResult,
    PlanResult,
    ProposedChange,
    ReviewFinding,
    ReviewResult,
    TestResult,
    Violation,
)
from agent_platform.github_reporting import GitHubReporter

_PLAN_HASH = "sha256:" + "a" * 64
_HEAD = "b" * 40


class FakeRecordingClient:
    """Minimal client double: records posts, serves canned gets."""

    def __init__(self, existing_comments: list | None = None) -> None:
        self.posts: list[tuple[str, dict]] = []
        self._existing = existing_comments or []

    def get(self, path):
        if "/comments" in path:
            return self._existing
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, payload):
        self.posts.append((path, payload))
        return {"id": 123}


def _reporter(existing=None) -> tuple[GitHubReporter, FakeRecordingClient]:
    client = FakeRecordingClient(existing)
    return GitHubReporter(client), client  # type: ignore[arg-type]


def _plan() -> PlanResult:
    return PlanResult(
        task_id="task-0123456789ab",
        repository="owner/repo",
        base_sha="c" * 40,
        summary="Add a tiny game module.",
        proposed_changes=[ProposedChange(path="game.py", reason="new module")],
        test_commands=["python -m unittest discover"],
    )


def _review() -> ReviewResult:
    return ReviewResult(
        task_id="task-0123456789ab",
        repository="owner/repo",
        plan_hash=_PLAN_HASH,
        base_sha="c" * 40,
        head_sha=_HEAD,
        summary="Implementation matches the plan.",
        findings=[
            ReviewFinding(
                severity="low", path="game.py", line=3,
                message="Consider extracting the loop.",
                evidence="+for i in range(10):",
            )
        ],
    )


def _execution() -> ExecutionResult:
    return ExecutionResult(
        task_id="task-0123456789ab",
        plan_hash=_PLAN_HASH,
        base_sha="c" * 40,
        status="succeeded",
        changed_files=["game.py"],
        commands=[CommandRecord(command="python -m unittest discover",
                                exit_code=0, duration_ms=10)],
        tests=[TestResult(command="python -m unittest discover", status="passed")],
    )


# --- plan comment ---

def test_plan_comment_posts_schema_backed_fields_only():
    reporter, client = _reporter()
    reporter.post_plan_comment(repository="owner/repo", pr_number=9,
                               plan=_plan(), plan_hash=_PLAN_HASH)
    (path, payload), = client.posts
    assert path == "/repos/owner/repo/issues/9/comments"
    body = payload["body"]
    assert _PLAN_HASH in body
    assert "game.py" in body
    assert "python -m unittest discover" in body


def test_plan_comment_shows_the_execute_network_when_given():
    """The approver sees what execute may reach before approving."""
    reporter, client = _reporter()
    reporter.post_plan_comment(repository="owner/repo", pr_number=9,
                               plan=_plan(), plan_hash=_PLAN_HASH, awaiting_human=True,
                               network="allowlist (pypi.org, files.pythonhosted.org)")
    (_, payload), = client.posts
    assert "- execute network: allowlist (pypi.org, files.pythonhosted.org)" in payload["body"]

    reporter, client = _reporter()
    reporter.post_plan_comment(repository="owner/repo", pr_number=9,
                               plan=_plan(), plan_hash=_PLAN_HASH)
    (_, payload), = client.posts
    assert "execute network" not in payload["body"]


def test_plan_comment_is_idempotent_on_same_plan():
    marker = f"<!-- agent-platform:result:{_PLAN_HASH}-->"
    reporter, client = _reporter(existing=[{"id": 1, "body": marker + "\nold"}])
    rc = reporter.post_plan_comment(repository="owner/repo", pr_number=9,
                                    plan=_plan(), plan_hash=_PLAN_HASH)
    assert rc == -1
    assert client.posts == []  # skipped


def test_plan_comment_does_not_leak_issue_body():
    """The builder only receives PlanResult fields — the issue body is not
    part of the model, so it cannot leak. Assert the body has no free-form
    section beyond the known fields."""
    reporter, client = _reporter()
    reporter.post_plan_comment(repository="owner/repo", pr_number=9,
                               plan=_plan(), plan_hash=_PLAN_HASH)
    body = client.posts[0][1]["body"]
    for forbidden in ("<issue>", "UNTRUSTED", "codex stdout"):
        assert forbidden not in body


# --- review check run ---

def test_review_check_run_success_when_no_findings():
    reporter, client = _reporter()
    review = _review()
    clean = review.model_copy(update={"findings": []})
    reporter.post_review_check(repository="owner/repo", pr_number=9,
                               head_sha=_HEAD, review=clean, plan_hash=_PLAN_HASH)
    (path, payload), = client.posts
    assert path == "/repos/owner/repo/check-runs"
    assert payload["head_sha"] == _HEAD
    assert payload["conclusion"] == "success"
    assert "No findings" in payload["output"]["text"]


def test_review_check_run_lists_findings_and_neutral_conclusion():
    reporter, client = _reporter()
    reporter.post_review_check(repository="owner/repo", pr_number=9,
                               head_sha=_HEAD, review=_review(), plan_hash=_PLAN_HASH)
    payload = client.posts[0][1]
    assert payload["conclusion"] == "neutral"
    text = payload["output"]["text"]
    assert "| low |" in text and "game.py" in text


def test_check_name_is_stable_per_repository_and_pr():
    reporter, client = _reporter()
    reporter.post_review_check(repository="owner/repo", pr_number=9,
                               head_sha=_HEAD, review=_review(), plan_hash=_PLAN_HASH)
    (path, payload), = client.posts
    assert payload["name"] == "agent-platform/review/owner-repo#9"


# --- execution check run ---

def test_execution_check_run_failure_on_violation():
    reporter, client = _reporter()
    execution = _execution().model_copy(update={
        "violations": [Violation(kind="out_of_scope_path", detail="evil.py")],
        "tests": [TestResult(command="t", status="failed")],
    })
    reporter.post_execution_check(repository="owner/repo", pr_number=9,
                                  head_sha=_HEAD, execution=execution,
                                  plan_hash=_PLAN_HASH)
    payload = client.posts[0][1]
    assert payload["conclusion"] == "failure"
    assert "out_of_scope_path" in payload["output"]["text"]
    assert "evil.py" in payload["output"]["text"]


def test_execution_check_run_success_path():
    reporter, client = _reporter()
    reporter.post_execution_check(repository="owner/repo", pr_number=9,
                                  head_sha=_HEAD, execution=_execution(),
                                  plan_hash=_PLAN_HASH)
    payload = client.posts[0][1]
    assert payload["conclusion"] == "success"
    assert "0 failed" in payload["output"]["summary"]


# --- collaborator permission gate (comment kick) ---

class _FakePermissionClient:
    def __init__(self, response: dict | Exception) -> None:
        self._response = response

    def get(self, path):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _reader_with(client) -> object:
    from pathlib import Path

    from agent_platform.adapters.github.reader import GithubReader

    key_path = Path(__file__).resolve().parents[1] / "fixtures" / "test-app-key.pem"
    _ = key_path  # the double never loads the key; only _client is exercised
    reader = GithubReader.__new__(GithubReader)
    reader._client = client
    return reader


def test_has_write_access_write_and_admin():
    reader = _reader_with(_FakePermissionClient({"permission": "write"}))
    assert reader.has_write_access("owner/repo", "alice") is True
    reader = _reader_with(_FakePermissionClient({"permission": "admin"}))
    assert reader.has_write_access("owner/repo", "root") is True


def test_has_write_access_read_or_none_denied():
    reader = _reader_with(_FakePermissionClient({"permission": "read"}))
    assert reader.has_write_access("owner/repo", "reader") is False
    reader = _reader_with(_FakePermissionClient({"permission": "none"}))
    assert reader.has_write_access("owner/repo", "nobody") is False


def test_has_write_access_fails_closed_on_api_error():
    from agent_platform.adapters.github.errors import GitHubError

    reader = _reader_with(_FakePermissionClient(GitHubError("api down")))
    assert reader.has_write_access("owner/repo", "alice") is False


# --- post-approval pipeline stop notice ---


def test_pipeline_stopped_notice_is_fixed_vocabulary():
    reporter, client = _reporter()
    reporter.post_pipeline_stopped(
        repository="owner/repo", issue_number=23, task_id="task-0123456789ab",
        stage="execute", status="NEEDS_REAPPROVAL",
    )
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/repos/owner/repo/issues/23/comments"
    assert payload["body"].startswith(
        "agent-platform: `task-0123456789ab` stopped at **execute** "
        "(status `NEEDS_REAPPROVAL`)."
    )


def test_pipeline_stopped_notice_refuses_free_text():
    import pytest

    reporter, client = _reporter()
    for stage, status, task_id in (
        ("execute", "boom: secret stack trace", "task-0123456789ab"),
        ("deploy", "FAILED", "task-0123456789ab"),
        ("execute", "FAILED", "task-<script>"),
    ):
        with pytest.raises(ValueError, match="unexpected"):
            reporter.post_pipeline_stopped(
                repository="owner/repo", issue_number=1, task_id=task_id,
                stage=stage, status=status,
            )
    assert client.posts == []


# --- issue-side plan comments (human decides on the issue) ---


def _asking_plan() -> PlanResult:
    return _plan().model_copy(update={
        "proposed_changes": [], "requires_human_input": True,
        "summary": "max_tokens is not specified.",
        "questions": ["Which max_tokens value?"],
    })


def test_issue_plan_comment_prompts_approve():
    reporter, client = _reporter()
    reporter.post_plan_comment(repository="owner/repo", pr_number=23, plan=_plan(),
                               plan_hash=_PLAN_HASH, awaiting_human=True)
    path, payload = client.posts[0]
    assert path == "/repos/owner/repo/issues/23/comments"
    assert "`game.py`" in payload["body"]
    assert payload["body"].endswith(
        "reply with `/agent-platform approve` (a collaborator with write access). "
        "Execute, the Draft PR and the review then run automatically."
    )


def test_issue_plan_comment_asks_for_answer():
    reporter, client = _reporter()
    reporter.post_plan_comment(repository="owner/repo", pr_number=23, plan=_asking_plan(),
                               plan_hash=_PLAN_HASH, awaiting_human=True)
    body = client.posts[0][1]["body"]
    assert "### agent-platform plan — input needed" in body
    assert "- Which max_tokens value?" in body
    assert "`/agent-platform answer <your answer>`" in body
    assert "/agent-platform approve" not in body


def test_pr_plan_comment_has_no_reply_prompt():
    reporter, client = _reporter()
    reporter.post_plan_comment(repository="owner/repo", pr_number=9, plan=_plan(),
                               plan_hash=_PLAN_HASH)
    body = client.posts[0][1]["body"]
    assert "/agent-platform approve" not in body
    assert "/agent-platform answer" not in body


def test_reply_prompt_survives_truncation():
    reporter, client = _reporter()
    long_plan = _plan().model_copy(update={"summary": "x" * 9000})
    reporter.post_plan_comment(repository="owner/repo", pr_number=23, plan=long_plan,
                               plan_hash=_PLAN_HASH, awaiting_human=True)
    body = client.posts[0][1]["body"]
    assert len(body) <= 4096
    assert body.endswith("then run automatically.")


def test_plan_failed_notice_is_fixed_vocabulary():
    import pytest

    reporter, client = _reporter()
    reporter.post_plan_failed(repository="owner/repo", issue_number=23,
                              task_id="task-0123456789ab")
    path, payload = client.posts[0]
    assert path == "/repos/owner/repo/issues/23/comments"
    assert "`task-0123456789ab` proposed no file changes" in payload["body"]
    assert "`/agent-platform redo`" in payload["body"]
    with pytest.raises(ValueError, match="unexpected task id"):
        reporter.post_plan_failed(repository="owner/repo", issue_number=23, task_id="x")


# --- command acknowledgement ---


def test_command_ack_names_command_author_and_next_step():
    reporter, client = _reporter()
    reporter.post_command_ack(repository="owner/repo", issue_number=23,
                              command="run", author="agis09")
    path, payload = client.posts[0]
    assert path == "/repos/owner/repo/issues/23/comments"
    assert payload["body"] == (
        "agent-platform: accepted `/agent-platform run` from @agis09. "
        "Planning now; the plan will be posted here."
    )


def test_command_ack_for_approve_names_the_task():
    reporter, client = _reporter()
    reporter.post_command_ack(repository="owner/repo", issue_number=23, command="approve",
                              author="agis09", task_id="task-0123456789ab")
    body = client.posts[0][1]["body"]
    assert body.startswith(
        "agent-platform: accepted `/agent-platform approve` for `task-0123456789ab` from @agis09."
    )
    assert "run automatically" in body


def test_command_ack_without_auto_pipeline_does_not_promise_execute():
    reporter, client = _reporter()
    reporter.post_command_ack(repository="owner/repo", issue_number=23, command="approve",
                              author="agis09", task_id="task-0123456789ab",
                              auto_pipeline=False)
    body = client.posts[0][1]["body"]
    assert "run automatically" not in body
    assert "`agentctl execute`" in body


def test_command_ack_refuses_free_text():
    import pytest

    reporter, client = _reporter()
    for command, author, task_id in (
        ("deploy", "agis09", None),
        ("run", "bad login\n@everyone", None),
        ("run", "x" * 40, None),
        ("approve", "agis09", "task-<script>"),
    ):
        with pytest.raises(ValueError, match="unexpected"):
            reporter.post_command_ack(repository="owner/repo", issue_number=1,
                                      command=command, author=author, task_id=task_id)
    assert client.posts == []


def test_command_ack_accepts_app_logins():
    reporter, client = _reporter()
    reporter.post_command_ack(repository="owner/repo", issue_number=1,
                              command="redo", author="renovate[bot]")
    assert "from @renovate[bot]." in client.posts[0][1]["body"]


# --- refused command notice ---


def test_command_rejected_points_a_waiting_plan_at_answer():
    reporter, client = _reporter()
    reporter.post_command_rejected(
        repository="owner/repo", issue_number=26, command="redo", author="agis09",
        task_id="task-e63c539db9a3", status="AWAITING_APPROVAL", needs_input=True,
    )
    path, payload = client.posts[0]
    assert path == "/repos/owner/repo/issues/26/comments"
    assert payload["body"] == (
        "agent-platform: `/agent-platform redo` from @agis09 was not applied. "
        "The latest task `task-e63c539db9a3` is `AWAITING_APPROVAL`. "
        "Its plan is waiting for answers: reply with "
        "`/agent-platform answer <answer>` to plan again with them."
    )


def test_command_rejected_without_known_state_points_at_the_log():
    reporter, client = _reporter()
    reporter.post_command_rejected(repository="owner/repo", issue_number=1,
                                   command="approve", author="agis09")
    assert client.posts[0][1]["body"] == (
        "agent-platform: `/agent-platform approve` from @agis09 was not applied. "
        "The reason is recorded in the orchestrator log."
    )


def test_command_rejected_names_the_command_specific_next_step():
    reporter, client = _reporter()
    reporter.post_command_rejected(
        repository="owner/repo", issue_number=1, command="answer", author="agis09",
        task_id="task-0123456789ab", status="COMPLETED",
    )
    assert "`/agent-platform redo`" in client.posts[0][1]["body"]


def test_command_rejected_refuses_free_text():
    import pytest

    reporter, client = _reporter()
    for command, author, task_id, status in (
        ("run", "agis09", None, None),
        ("redo", "bad login\n@everyone", None, None),
        ("redo", "agis09", "task-<script>", "FAILED"),
        ("redo", "agis09", "task-0123456789ab", "failed; @everyone"),
    ):
        with pytest.raises(ValueError, match="unexpected"):
            reporter.post_command_rejected(
                repository="owner/repo", issue_number=1, command=command,
                author=author, task_id=task_id, status=status,
            )
    assert client.posts == []


# --- issue-side Draft PR notice ---


def test_pr_created_notice_links_the_pr():
    reporter, client = _reporter()
    reporter.post_pr_created(repository="owner/repo", issue_number=23,
                             task_id="task-0123456789ab", pr_number=29)
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/repos/owner/repo/issues/23/comments"
    assert payload["body"] == (
        "<!-- agent-platform:pr-created:task-0123456789ab-->\n"
        "agent-platform: Draft PR #29 was created for `task-0123456789ab`."
    )


def test_pr_created_notice_is_idempotent_per_task():
    existing = [{"body": "<!-- agent-platform:pr-created:task-0123456789ab-->\nold"}]
    reporter, client = _reporter(existing)
    comment_id = reporter.post_pr_created(repository="owner/repo", issue_number=23,
                                          task_id="task-0123456789ab", pr_number=29)
    assert comment_id == -1
    assert client.posts == []


def test_pr_created_notice_refuses_unexpected_values():
    import pytest

    reporter, client = _reporter()
    for task_id, pr_number in (("task-<script>", 29), ("task-0123456789ab", 0)):
        with pytest.raises(ValueError, match="unexpected"):
            reporter.post_pr_created(repository="owner/repo", issue_number=23,
                                     task_id=task_id, pr_number=pr_number)
    assert client.posts == []


# --- review content as a PR comment ---


def test_review_comment_carries_summary_and_findings():
    reporter, client = _reporter()
    reporter.post_review_comment(repository="owner/repo", pr_number=31, head_sha=_HEAD,
                                 review=_review(), plan_hash=_PLAN_HASH)
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/repos/owner/repo/issues/31/comments"
    body = payload["body"]
    assert body.startswith(f"<!-- agent-platform:review:task-0123456789ab:{_HEAD}-->\n")
    assert "### agent-platform review — 1 finding(s)" in body
    assert "Implementation matches the plan." in body
    assert "| low | `game.py:3` | Consider extracting the loop. |" in body


def test_review_comment_clean_review():
    reporter, client = _reporter()
    review = _review().model_copy(update={"findings": []})
    reporter.post_review_comment(repository="owner/repo", pr_number=31, head_sha=_HEAD,
                                 review=review, plan_hash=_PLAN_HASH)
    body = client.posts[0][1]["body"]
    assert "0 finding(s)" in body
    assert "No findings — review clean." in body


def test_review_comment_is_idempotent_per_head():
    existing = [{"body": f"<!-- agent-platform:review:task-0123456789ab:{_HEAD}-->\nold"}]
    reporter, client = _reporter(existing)
    assert reporter.post_review_comment(repository="owner/repo", pr_number=31,
                                        head_sha=_HEAD, review=_review(),
                                        plan_hash=_PLAN_HASH) == -1
    assert client.posts == []
    # A new head is a new review: posted.
    reporter.post_review_comment(repository="owner/repo", pr_number=31,
                                 head_sha="d" * 40, review=_review(), plan_hash=_PLAN_HASH)
    assert len(client.posts) == 1


def test_review_comment_defuses_mentions_and_table_breaks():
    reporter, client = _reporter()
    review = _review().model_copy(update={
        "summary": "ping @alice",
        "findings": [ReviewFinding(severity="high", path="a|b.py", line=None,
                                   message="x | y\n@bob z", evidence="e")],
    })
    reporter.post_review_comment(repository="owner/repo", pr_number=31, head_sha=_HEAD,
                                 review=review, plan_hash=_PLAN_HASH)
    body = client.posts[0][1]["body"]
    assert "@alice" not in body and "@bob" not in body
    assert "ping @​alice" in body
    assert "| high | `a\\|b.py` | x \\| y @​bob z |" in body


def test_review_comment_refuses_bad_head():
    import pytest

    reporter, client = _reporter()
    with pytest.raises(ValueError, match="unexpected head sha"):
        reporter.post_review_comment(repository="owner/repo", pr_number=31,
                                     head_sha="HEAD", review=_review(),
                                     plan_hash=_PLAN_HASH)
    assert client.posts == []


def test_cli_review_posts_comment_even_if_check_fails(monkeypatch):
    from types import SimpleNamespace

    from agent_platform import cli

    class _Reporter:
        def __init__(self):
            self.comments = []

        def post_review_check(self, **kwargs):
            raise RuntimeError("checks api down")

        def post_review_comment(self, **kwargs):
            self.comments.append(kwargs)
            return 7

    rec = _Reporter()
    monkeypatch.setattr(cli, "_build_reporter", lambda: rec)
    monkeypatch.setattr(cli, "_task_repository_from_service", lambda s, t: "owner/repo")
    outcome = SimpleNamespace(review=_review())
    cli._post_review_check(None, outcome, "task-0123456789ab", _PLAN_HASH, 31, _HEAD)
    assert len(rec.comments) == 1
    assert rec.comments[0]["pr_number"] == 31
    assert rec.comments[0]["head_sha"] == _HEAD
