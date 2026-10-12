"""Contract: forbidden tokens flow plan → task → approval → execute spec,
and the worker-side search CLI enforces them."""

import json

import pytest

from agent_platform.search.cli import main as search_main
from tests.conftest import BASE_SHA


def test_plan_stores_forbidden_tokens(repository, artifacts, clock, settings):
    """issue-derived tokens land on the task at plan time."""
    service = _service(repository, artifacts, clock, settings)
    outcome = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA,
        issue_title="Fix invaders.py rendering",
        issue_body="The Game.draw method is broken for wave logic.",
    )
    task = repository.get_task(outcome.task.task_id)
    assert "invaders.py" in task.search_forbidden_tokens
    assert "game.draw" in task.search_forbidden_tokens


def test_approval_carries_tokens_to_execute_spec(repository, artifacts, clock, settings):
    """approve() copies the task's tokens onto the approval; the execute spec
    receives them."""
    service = _service(repository, artifacts, clock, settings)
    outcome = service.plan(
        repository="owner/repo", issue_number=1, base_branch="main", base_sha=BASE_SHA,
        issue_title="Fix invaders.py rendering",
        issue_body="The Game.draw method is broken.",
    )
    task_id = outcome.task.task_id
    plan_hash = outcome.task.plan_hash
    service.approve(task_id=task_id, plan_hash=plan_hash, approver="tester")
    approval = repository.find_approval(task_id, plan_hash)
    assert approval is not None
    assert "invaders.py" in approval.search_forbidden_tokens

    # The execute path accepts the spec carrying the approval's tokens.
    service.execute(task_id=task_id, plan_hash=plan_hash)
    assert repository.get_task(task_id).status.value in ("EXECUTING", "READY_TO_PUBLISH")


@pytest.fixture
def fake_search(monkeypatch):
    # The search CLI is disabled by default; these tests exercise the
    # sanitizer behind an enabled (offline) provider.
    monkeypatch.setenv("SEARCH_PROVIDER", "fake")


def test_search_cli_is_disabled_by_default(monkeypatch, capsys):
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("EXEC_SEARCH_FORBIDDEN", "[]")
    rc = search_main(["python curses terminal tutorial"])
    assert rc == 1
    assert "search refused: search is disabled in this deployment" in capsys.readouterr().err


def test_search_cli_refuses_forbidden_token(monkeypatch, capsys, fake_search):
    monkeypatch.setenv(
        "EXEC_SEARCH_FORBIDDEN",
        json.dumps(["invaders.py", "game.draw"]),
    )
    rc = search_main(["how does game.draw work"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "refused" in err
    assert "disabled" not in err


def test_search_cli_accepts_clean_query(monkeypatch, capsys, fake_search):
    monkeypatch.setenv("EXEC_SEARCH_FORBIDDEN", json.dumps(["invaders.py"]))
    rc = search_main(["python curses terminal tutorial"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "fake"
    assert payload["hits"]


def test_search_cli_refuses_secret_shaped_query(monkeypatch, capsys, fake_search):
    monkeypatch.setenv("EXEC_SEARCH_FORBIDDEN", "[]")
    rc = search_main(["is ghp_AbcdefghijKlmnopqrstuv a valid token format"])
    assert rc == 1
    assert "disabled" not in capsys.readouterr().err


def _service(repository, artifacts, clock, settings):
    from agent_platform.orchestrator.service import TaskService

    return TaskService(
        repository=repository,
        adapter=_RecordingAdapter(),
        artifacts=artifacts,
        clock=clock,
        settings=settings,
    )


class _RecordingAdapter:
    """Plan adapter returning a minimal valid plan; execute is never reached
    in these tests beyond spec construction."""

    def plan(self, job):
        from agent_platform.adapters.agent.fake import FakeAgentAdapter

        return FakeAgentAdapter().plan(job)

    def execute(self, job):
        from agent_platform.adapters.agent.fake import FakeAgentAdapter

        return FakeAgentAdapter().execute(job)

    def review(self, job):
        raise NotImplementedError
