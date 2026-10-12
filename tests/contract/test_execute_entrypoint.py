"""The execute worker (agent_platform.worker.execute), run for real against
a temp git snapshot with a fake `codex` on PATH (no network, no container).

- interpreter by-products (__pycache__/*.pyc) are not changes
- scope violations keep status "succeeded" -> orchestrator NEEDS_REAPPROVAL
- a failed codex run is status "failed" with a recorded violation, and the
  runner still writes a result (exit 0)
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_platform.domain.models import ExecutionResult

_ROOT = Path(__file__).resolve().parents[2]
_PLAN_HASH = "sha256:" + "a" * 64

_FAKE_CODEX = """#!/bin/sh
# Fake codex: consume the prompt, apply the scripted edits, emit one event.
cat > /dev/null
{edits}
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"done"}}}}'
exit {exit_code}
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,  # noqa: S607
    ).stdout.strip()


def _run(tmp_path: Path, *, edits: str, exit_code: int = 0, allowed: str = "app.py",
         tests: str = ""):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('v1')\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(_FAKE_CODEX.format(edits=edits, exit_code=exit_code))
    codex.chmod(0o755)
    out = tmp_path / "out"
    env = {
        "PATH": f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(_ROOT / "src"),
        "TMPDIR": str(tmp_path),
        "EXEC_REPO_DIR": str(repo),
        "EXEC_OUT_DIR": str(out),
        "EXEC_TASK_ID": "task-0123456789ab",
        "EXEC_PLAN_HASH": _PLAN_HASH,
        "EXEC_BASE_SHA": base_sha,
        "EXEC_ALLOWED_PATHS": allowed,
        "EXEC_TEST_COMMANDS": tests,
    }
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "agent_platform.worker.execute"],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    result_path = out / "execution_result.json"
    assert result_path.exists(), proc.stderr
    return proc, json.loads(result_path.read_text()), (out / "patch.diff").read_text()


@pytest.mark.skipif(os.name != "posix", reason="POSIX fake agent script")
def test_bytecode_is_not_a_change(tmp_path):
    proc, result, patch = _run(tmp_path, edits=(
        "echo \"print('v2')\" > app.py\n"
        "mkdir -p __pycache__ pkg/__pycache__\n"
        "printf 'x' > __pycache__/app.cpython-312.pyc\n"
        "printf 'x' > pkg/__pycache__/m.cpython-312.pyc\n"
    ))
    assert proc.returncode == 0, proc.stderr
    assert result["status"] == "succeeded"
    assert result["changed_files"] == ["app.py"]
    assert result["violations"] == []
    assert ".pyc" not in patch
    assert "+print('v2')" in patch
    ExecutionResult.model_validate(result)


@pytest.mark.skipif(os.name != "posix", reason="POSIX fake agent script")
def test_scope_violation_keeps_succeeded_status(tmp_path):
    proc, result, _ = _run(tmp_path, edits=(
        "echo \"print('v2')\" > app.py\n"
        "echo 'oops' > other.txt\n"
    ))
    assert proc.returncode == 0, proc.stderr
    # succeeded + violations -> the orchestrator routes to NEEDS_REAPPROVAL
    assert result["status"] == "succeeded"
    assert result["violations"] == [{"kind": "out_of_scope_path", "detail": "other.txt"}]


@pytest.mark.skipif(os.name != "posix", reason="POSIX fake agent script")
def test_failed_codex_run_is_recorded_not_crashed(tmp_path):
    proc, result, _ = _run(tmp_path, edits="", exit_code=1)
    assert proc.returncode == 0, proc.stderr
    assert "NameError" not in proc.stderr
    assert result["status"] == "failed"
    assert result["violations"] == [{"kind": "other", "detail": "codex exited 1"}]


@pytest.mark.skipif(os.name != "posix", reason="POSIX fake agent script")
def test_user_installed_tools_are_on_path(tmp_path):
    """`pip install --user` puts pytest in ~/.local/bin; the approved
    test command must find it by name."""
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    tool = local_bin / "p18-user-tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    proc, result, _ = _run(tmp_path, edits="echo \"print('v2')\" > app.py\n",
                           tests="p18-user-tool")
    assert proc.returncode == 0, proc.stderr
    assert result["tests"] == [{"command": "p18-user-tool", "status": "passed"}]
