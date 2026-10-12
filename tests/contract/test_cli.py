"""Contract: CLI end-to-end without network — plan duplicate protection,
approve flows, exit codes."""

import json
import subprocess
from pathlib import Path

BASE_SHA = "a" * 40

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VENV_PYTHON = _PROJECT_ROOT / ".venv" / "bin" / "python"


def run_cli(*args: str, home: Path, env_extra: dict[str, str] | None = None):
    env = {
        "AGENT_PLATFORM_HOME": str(home),
        "PATH": "/usr/bin:/bin",
    }
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(  # noqa: S603
        [str(_VENV_PYTHON), "-m", "agent_platform.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=_PROJECT_ROOT,
    )
    return proc


def _plan(home: Path, repo="cli-test/repo", issue=1):
    return run_cli(
        "plan", "--repo", repo, "--issue", str(issue), "--base-sha", BASE_SHA, home=home
    )


def test_plan_then_replan_no_duplicate(tmp_path):
    first = _plan(tmp_path)
    assert first.returncode == 0, first.stderr
    second = _plan(tmp_path)
    assert second.returncode == 0, second.stderr
    first_out = first.stdout
    second_out = second.stdout
    assert "duplicate:     false" in first_out
    assert "duplicate:     true" in second_out
    task_id_a = first_out.splitlines()[0].split()[-1]
    task_id_b = second_out.splitlines()[0].split()[-1]
    assert task_id_a == task_id_b


def test_plan_json_output(tmp_path):
    proc = run_cli(
        "plan", "--repo", "cli-test/repo", "--issue", "2", "--base-sha", BASE_SHA,
        "--json", home=tmp_path,
    )
    payload = json.loads(proc.stdout)
    assert payload["status"] == "AWAITING_APPROVAL"
    assert payload["plan_hash"].startswith("sha256:")
    assert payload["duplicate"] is False


def test_full_flow_to_approved(tmp_path):
    plan_out = run_cli(
        "plan", "--repo", "cli-test/repo", "--issue", "3", "--base-sha", BASE_SHA,
        "--json", home=tmp_path,
    )
    payload = json.loads(plan_out.stdout)
    task_id = payload["task_id"]
    plan_hash = payload["plan_hash"]

    wrong = run_cli(
        "approve", "--task-id", task_id, "--plan-hash", "sha256:" + "0" * 64,
        "--approver", "alice", home=tmp_path,
    )
    assert wrong.returncode == 3

    approved = run_cli(
        "approve", "--task-id", task_id, "--plan-hash", plan_hash,
        "--approver", "alice", "--json", home=tmp_path,
    )
    assert approved.returncode == 0, approved.stderr
    assert json.loads(approved.stdout)["status"] == "APPROVED"

    duplicate = run_cli(
        "approve", "--task-id", task_id, "--plan-hash", plan_hash,
        "--approver", "alice", home=tmp_path,
    )
    assert duplicate.returncode == 0
    assert "duplicate:    true" in duplicate.stdout

    status = run_cli("status", "--task-id", task_id, "--json", home=tmp_path)
    view = json.loads(status.stdout)
    assert view["status"] == "APPROVED"
    assert any(e["to"] == "APPROVED" for e in view["events"])


def test_unknown_task_exit_code(tmp_path):
    proc = run_cli(
        "approve", "--task-id", "task-000000000000",
        "--plan-hash", "sha256:" + "0" * 64, "--approver", "alice", home=tmp_path,
    )
    assert proc.returncode == 4

    status = run_cli("status", "--task-id", "task-000000000000", home=tmp_path)
    assert status.returncode == 4


def test_bad_args_exit_code(tmp_path):
    proc = run_cli(
        "plan", "--repo", "cli-test/repo", "--issue", "1", "--base-sha", "short", home=tmp_path
    )
    assert proc.returncode == 2
    proc = run_cli(
        "plan", "--repo", "invalid", "--issue", "1", "--base-sha", BASE_SHA, home=tmp_path
    )
    assert proc.returncode == 2


def test_cli_error_output_redacts_planted_secret(tmp_path, monkeypatch):
    env_extra = {"GITHUB_TOKEN": "ghp_CanaryCli0123456789"}
    # trigger a domain error whose message path passes through redaction
    proc = run_cli(
        "approve", "--task-id", "task-000000000000",
        "--plan-hash", "sha256:" + "0" * 64, "--approver", "alice",
        home=tmp_path, env_extra=env_extra,
    )
    assert "ghp_CanaryCli0123456789" not in proc.stderr


def test_reject_flow_to_cancelled(tmp_path):
    plan_out = run_cli(
        "plan", "--repo", "cli-test/repo", "--issue", "4", "--base-sha", BASE_SHA,
        "--json", home=tmp_path,
    )
    payload = json.loads(plan_out.stdout)
    task_id = payload["task_id"]
    plan_hash = payload["plan_hash"]

    rejected = run_cli(
        "reject", "--task-id", task_id, "--plan-hash", plan_hash,
        "--approver", "bob", "--reason", "not now", "--json", home=tmp_path,
    )
    assert rejected.returncode == 0, rejected.stderr
    body = json.loads(rejected.stdout)
    assert body["status"] == "CANCELLED"
    assert body["duplicate"] is False

    duplicate = run_cli(
        "reject", "--task-id", task_id, "--plan-hash", plan_hash,
        "--approver", "bob", home=tmp_path,
    )
    assert duplicate.returncode == 0
    assert "duplicate:    true" in duplicate.stdout

    # Reject after approve is a state conflict (exit 3).
    plan2 = run_cli(
        "plan", "--repo", "cli-test/repo", "--issue", "5", "--base-sha", BASE_SHA,
        "--json", home=tmp_path,
    )
    payload2 = json.loads(plan2.stdout)
    run_cli(
        "approve", "--task-id", payload2["task_id"],
        "--plan-hash", payload2["plan_hash"], "--approver", "alice", home=tmp_path,
    )
    late = run_cli(
        "reject", "--task-id", payload2["task_id"],
        "--plan-hash", payload2["plan_hash"], "--approver", "bob", home=tmp_path,
    )
    assert late.returncode == 3


def test_reopen_cli_refuses_task_not_ready_to_publish(tmp_path):
    plan_out = run_cli(
        "plan", "--repo", "cli-test/repo", "--issue", "9", "--base-sha", BASE_SHA,
        "--json", home=tmp_path,
    )
    payload = json.loads(plan_out.stdout)
    proc = run_cli(
        "reopen", "--task-id", payload["task_id"], "--plan-hash", payload["plan_hash"],
        "--requested-by", "bob", "--reason", "empty patch", home=tmp_path,
    )
    assert proc.returncode == 3, proc.stderr
    assert "not READY_TO_PUBLISH" in proc.stderr
    status = run_cli("status", "--task-id", payload["task_id"], "--json", home=tmp_path)
    assert json.loads(status.stdout)["status"] == "AWAITING_APPROVAL"
