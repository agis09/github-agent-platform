"""Execute worker: apply an APPROVED plan inside the repository snapshot.

Inputs (env):
  EXEC_TASK_ID / EXEC_PLAN_HASH / EXEC_BASE_BRANCH / EXEC_BASE_SHA
  EXEC_ALLOWED_PATHS   — newline-separated allowlist from the approval
  EXEC_TEST_COMMANDS   — newline-separated test commands from the approval
  EXEC_COMMAND_TIMEOUT — per-command timeout seconds
  EXEC_MAX_OUTPUT      — max patch bytes
  EXEC_PROMPT          — execute prompt (plan + scope + network notes)
  EXEC_S3_RESULT_URL / EXEC_S3_PATCH_URL — optional presigned PUT URLs
  AGENT_PLATFORM_AGENT_* — runtime settings (config.py)
  /workspace/repo      — repository snapshot at base SHA (git dir included)
Output: /out/execution_result.json, /out/patch.diff and the log blocks.

Security: the container has no git push credentials and no GitHub token;
pushing is impossible by construction. The Publisher (outside the
sandbox, no LLM) re-validates the patch before creating the PR.

Exit codes: 0 = a result was written (execution_result.json "status" is
the single source of truth; violations travel inside it and route the task
to NEEDS_REAPPROVAL). Non-zero = no usable result (snapshot mismatch,
agent timeout).
"""

import glob
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_platform.agent_runtime import RunMode, build_runtime
from agent_platform.config import Settings
from agent_platform.errors import AdapterError, AdapterTimeoutError

# Interpreter by-products are never part of a plan: an agent validating its
# work by running Python drops __pycache__/*.pyc next to the sources. They
# are excluded via a runner-owned excludes file (`git -c core.excludesFile`)
# — the snapshot's own .gitignore / .git are untouched, and TRACKED files
# are unaffected (excludes only hide untracked files).
GENERATED_EXCLUDES = "__pycache__/\n*.py[cod]\n"


def main() -> int:
    repo = Path(os.environ.get("EXEC_REPO_DIR", "/workspace/repo"))
    out = Path(os.environ.get("EXEC_OUT_DIR", "/out"))
    max_output = int(os.environ.get("EXEC_MAX_OUTPUT", str(1024 * 1024)))
    cmd_timeout = float(os.environ.get("EXEC_COMMAND_TIMEOUT", "120"))
    allowed = [p for p in os.environ.get("EXEC_ALLOWED_PATHS", "").split("\n") if p]
    tests = [c for c in os.environ.get("EXEC_TEST_COMMANDS", "").split("\n") if c]

    def run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 — fixed git / approved test argv
            cmd, capture_output=True, cwd=repo, timeout=cmd_timeout, check=False
        )

    # User-level installs (`pip install --user` at egress level allowlist)
    # put console scripts in ~/.local/bin; the agent and the approved test
    # commands must find them by name.
    home = os.environ.get("HOME", "/home/worker")
    os.environ["PATH"] = f"{home}/.local/bin:{os.environ.get('PATH', '')}"

    settings = Settings.from_environ()
    runtime = build_runtime(settings)
    scratch = Path(tempfile.mkdtemp(prefix="runner-"))
    excludes = scratch / "excludes"
    excludes.write_text(GENERATED_EXCLUDES)
    git = ["git", "-c", f"core.excludesFile={excludes}"]

    # 0. Snapshot is required and must be at the frozen base SHA.
    head = run(["git", "rev-parse", "HEAD"]).stdout.decode().strip()
    base_sha = os.environ["EXEC_BASE_SHA"]
    if head != base_sha:
        print(f"runner: snapshot HEAD {head!r} != base_sha {base_sha!r}", file=sys.stderr)
        return 1

    # 1. Run the agent over the snapshot (write mode; the Pod is the sandbox).
    try:
        outcome = runtime.run(
            prompt=os.environ.get("EXEC_PROMPT", ""), mode=RunMode.WRITE, cwd=repo
        )
    except AdapterTimeoutError as exc:
        print(f"runner: {exc}", file=sys.stderr)
        out.mkdir(parents=True, exist_ok=True)
        (out / "execution_result.json").write_text(json.dumps({"status": "timeout"}))
        return 1
    except AdapterError as exc:
        print(f"runner: {exc}", file=sys.stderr)
        return 1

    agent_failed = not outcome.ok
    print(f"runner: {outcome.audit_line()}", file=sys.stderr)
    if agent_failed:
        print(
            f"runner: {outcome.runtime} failed: {outcome.failure_detail(2000)}",
            file=sys.stderr,
        )

    # 2. Collect changes: per-file scope check + patch (size-limited).
    status = run([*git, "status", "--porcelain"]).stdout.decode()
    changed: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip('"')
        # Porcelain reports untracked DIRECTORIES as "?? dir/"; expand them
        # so the per-file scope check sees every file.
        if line.startswith("??") and path.endswith("/"):
            changed.extend(
                sorted(
                    os.path.relpath(f, repo)
                    for f in glob.glob(f"{repo}/{path}**", recursive=True)
                    if Path(f).is_file()
                )
            )
        else:
            changed.append(path)
    changed = sorted(set(changed))
    violations: list[dict[str, str]] = [
        {"kind": "out_of_scope_path", "detail": p}
        for p in changed
        if not any(p == a or p.startswith(a.rstrip("/") + "/") for a in allowed)
    ]
    if agent_failed:
        detail = (
            f"{outcome.runtime} exited {outcome.returncode}"
            if outcome.returncode
            else f"{outcome.runtime} reported an error"
        )
        violations.append({"kind": "other", "detail": detail})
    # `git diff HEAD` omits untracked files: stage everything first so new
    # files are in the patch (the index is throwaway — rebuilt per run).
    run([*git, "add", "-A"])
    patch = run(["git", "diff", "HEAD"]).stdout
    if len(patch) > max_output:
        violations.append({"kind": "output_size", "detail": f"patch {len(patch)} bytes"})
        patch = patch[:max_output]

    # 3. Run the approved test commands; every command/exit code is audited.
    commands_audit: list[dict[str, Any]] = []
    test_results: list[dict[str, str]] = []
    for cmd in tests:
        parts = shlex.split(cmd)
        started = time.monotonic()
        try:
            proc = run(parts)
            code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except (subprocess.TimeoutExpired, OSError) as exc:
            code, stdout, stderr = -1, b"", str(exc).encode()
        commands_audit.append({
            "command": cmd,
            "exit_code": code,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout_text": stdout[:4096].decode(errors="replace"),
            "stderr_text": stderr[:4096].decode(errors="replace"),
        })
        test_results.append({"command": cmd, "status": "passed" if code == 0 else "failed"})

    result = {
        "schema_version": "1",
        "task_id": os.environ["EXEC_TASK_ID"],
        "plan_hash": os.environ["EXEC_PLAN_HASH"],
        "base_sha": base_sha,
        # "failed" = the agent run itself failed. Scope violations keep
        # "succeeded": the orchestrator routes succeeded+violations to
        # NEEDS_REAPPROVAL, and publish refuses any violated result.
        "status": "failed" if agent_failed else "succeeded",
        "changed_files": changed,
        "commands": commands_audit,
        "tests": test_results,
        "violations": violations,
        "pr_body_draft": f"Implements plan {os.environ['EXEC_PLAN_HASH']}",
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "execution_result.json").write_text(json.dumps(result, indent=2) + "\n")
    (out / "patch.diff").write_bytes(patch)
    print("EXECUTION_RESULT_BEGIN")
    print(json.dumps(result))
    print("EXECUTION_RESULT_END")
    print("PATCH_BEGIN")
    sys.stdout.flush()
    sys.stdout.buffer.write(patch)
    print()
    print("PATCH_END")

    _put_presigned(result, patch)
    return 0


def _put_presigned(result: dict[str, Any], patch: bytes) -> None:
    """Direct S3 put via presigned URLs (the worker holds no long-lived
    credentials). Absent URLs = offline/dev; the log markers remain the
    collection path."""
    targets = {
        "execution_result.json": (os.environ.get("EXEC_S3_RESULT_URL", ""),
                                  json.dumps(result).encode()),
        "patch.diff": (os.environ.get("EXEC_S3_PATCH_URL", ""), patch),
    }
    for name, (put_url, payload) in targets.items():
        if not put_url:
            continue
        request = urllib.request.Request(  # noqa: S310 — https URL minted by the launcher
            put_url, data=payload, method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310
                print(f"runner: s3 put {name} -> {resp.status}", file=sys.stderr)
        except (urllib.error.URLError, OSError) as exc:
            print(f"runner: s3 put {name} failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
