"""Contract: no secret-shaped canary values appear in captured logs across a
full CLI cycle. Canary is planted in (a) env, (b) issue body context the
adapter would see, (c) adapter-produced summary."""

import io
import logging
import subprocess
from pathlib import Path

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.clock import FixedClock
from agent_platform.config import Settings
from agent_platform.orchestrator.repository import SqliteTaskRepository
from agent_platform.orchestrator.service import TaskService
from agent_platform.redaction import configure_logging

CANARY_TOKEN = "ghp_CanaryLeak0123456789"
CANARY_ISSUE = "canary-issue-body-9f8e7d6c"
CANARY_SUMMARY = "canary-adapter-summary-1a2b3c4d"
BASE_SHA = "a" * 40


class LeakyAdapter(FakeAgentAdapter):
    """Simulates an adapter that echoes untrusted issue content."""

    def plan(self, job):
        result = super().plan(job)
        return result.model_copy(
            update={"summary": f"{result.summary} issue={CANARY_ISSUE} out={CANARY_SUMMARY}"}
        )


@pytest.fixture
def captured_logs():
    stream = io.StringIO()
    configure_logging("DEBUG", env={"GITHUB_TOKEN": CANARY_TOKEN}, stream=stream)
    yield stream


def test_full_cycle_logs_contain_no_canaries(tmp_path, captured_logs):
    settings = Settings(
        home_dir=str(tmp_path),
        artifact_max_size=1024 * 1024,
        default_approval_ttl_hours=72,
        agent_binary="codex",
        agent_model=None,
        agent_base_url=None,
        agent_api_key_env="LLM_API_KEY",
        codex_wire_api="responses",
        agent_timeout_seconds=600.0,
        agent_max_output_tokens=None,
    )
    repository = SqliteTaskRepository(tmp_path / "state.sqlite3")
    artifacts = LocalFsArtifactStore(tmp_path / "artifacts", settings.artifact_max_size)
    service = TaskService(
        repository=repository,
        adapter=LeakyAdapter(),
        artifacts=artifacts,
        clock=FixedClock(),
        settings=settings,
    )

    logging.getLogger("test").info("starting plan with token %s", CANARY_TOKEN)
    outcome = service.plan(
        repository="leak/test", issue_number=1, base_branch="main", base_sha=BASE_SHA
    )
    logging.getLogger("test").info("planned: %s", outcome.task.task_id)
    assert CANARY_ISSUE in outcome.plan.summary  # adapter DID receive the canary

    approved = service.approve(
        task_id=outcome.task.task_id, plan_hash=outcome.task.plan_hash, approver="alice"
    )
    logging.getLogger("test").info("approved by %s", approved.approval.approver)
    view = service.status(task_id=outcome.task.task_id)
    logging.getLogger("test").info("status %s events=%s", view.task.status, view.events)

    captured = captured_logs.getvalue()
    assert CANARY_TOKEN not in captured
    assert CANARY_ISSUE not in captured
    assert CANARY_SUMMARY not in captured
    assert "[REDACTED]" in captured  # the token was caught by pattern/env scan
    repository.close()


def test_subprocess_cli_cycle_logs_no_secrets(tmp_path):
    """The CLI error path also redacts — trigger a state conflict."""
    env = {"AGENT_PLATFORM_HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
           "GITHUB_TOKEN": CANARY_TOKEN}
    project_root = Path(__file__).resolve().parents[2]
    venv_python = project_root / ".venv" / "bin" / "python"
    base = [str(venv_python), "-m", "agent_platform.cli"]
    subprocess.run(  # noqa: S603
        base + ["plan", "--repo", "leak/test", "--issue", "1", "--base-sha", BASE_SHA],
        capture_output=True, text=True, env=env,
        cwd=project_root,
    )
    proc = subprocess.run(  # noqa: S603
        base + ["approve", "--task-id", "task-000000000000",
                "--plan-hash", "sha256:" + "0" * 64, "--approver", "alice"],
        capture_output=True, text=True, env=env,
        cwd=project_root,
    )
    assert proc.returncode == 4
    assert CANARY_TOKEN not in proc.stderr
    assert CANARY_TOKEN not in proc.stdout
