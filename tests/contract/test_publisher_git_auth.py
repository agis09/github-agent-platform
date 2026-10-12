"""Publisher git push credential handling.

The installation token must never appear in a git child process's argv
(readable by other processes via ps / /proc/<pid>/cmdline). It travels in the
GIT_CONFIG_* environment of the clone/push processes only.

A wrapper stands in for the git binary: it records argv and the GIT_CONFIG_*
environment of every invocation, then runs the real git with the
https://<host>/<repo>.git URL redirected to a local bare repository, so clone
and push run for real without network access.
"""

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_platform.adapters.github import GitHubApiClient, GitHubError
from agent_platform.adapters.github.reader import GitHubAppConfig
from agent_platform.domain.models import PublishRequest
from agent_platform.publisher.publisher import PullRequestPublisher

_KEY_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "test-app-key.pem"
_CANARY = "ghs_CANARY_publisher_token_7f3a9c"
_HOST = "ghe.example.com"
_REPO = "owner/repo"
_REAL_GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(_REAL_GIT is None, reason="git binary required")


def _git(*args: str, cwd: Path) -> str:
    assert _REAL_GIT is not None
    proc = subprocess.run(  # noqa: S603
        [_REAL_GIT, *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    return proc.stdout.strip()


def _publisher() -> PullRequestPublisher:
    config = GitHubAppConfig(
        api_base_url=f"https://{_HOST}/api/v3",
        app_id=1,
        installation_id=1,
        private_key_path=str(_KEY_PATH),
    )
    client = GitHubApiClient(config)
    client._fetch_installation_token = lambda: _CANARY  # type: ignore[method-assign]
    return PullRequestPublisher(client)


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[
    Path, Path, PublishRequest
]:
    """Bare 'remote' with one commit, a patch against it, and the git
    wrapper installed in place of shutil.which('git')."""
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    _git("clone", "-q", str(bare), str(seed), cwd=tmp_path)
    (seed / "a.txt").write_text("one\n")
    _git("add", "a.txt", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=seed)
    base_sha = _git("rev-parse", "HEAD", cwd=seed)
    (seed / "a.txt").write_text("one\ntwo\n")
    patch = _git("diff", cwd=seed) + "\n"

    log = tmp_path / "git-calls.jsonl"
    wrapper = tmp_path / "fake-git"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env': {\n"
        "        k: v for k, v in os.environ.items() if k.startswith('GIT_CONFIG')\n"
        "    }}) + '\\n')\n"
        f"os.execv({_REAL_GIT!r}, [{_REAL_GIT!r}, '-c',\n"
        f"    'url.file://{bare}.insteadOf=https://{_HOST}/{_REPO}.git',\n"
        "    *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: str(wrapper))

    request = PublishRequest(
        task_id="task-canary",
        repository=_REPO,
        plan_hash="sha256:" + "a" * 64,
        base_branch="main",
        base_sha=base_sha,
        patch=patch,
        changed_files=["a.txt"],
        approver="alice",
        approval_id="appr-1",
        approved_at="2026-10-06T00:00:00Z",
        expires_at="2026-10-07T00:00:00Z",
    )
    return bare, log, request


def _calls(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_token_not_in_git_argv_and_push_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, log, request = _setup(tmp_path, monkeypatch)
    pub = _publisher()
    branch = pub.branch_name(request.task_id)

    sha = pub._commit_patch(request, branch, request.base_sha)

    assert _git("rev-parse", f"refs/heads/{branch}", cwd=bare) == sha
    header = pub._auth_header()
    encoded = header.removeprefix("AUTHORIZATION: basic ")
    calls = _calls(log)
    assert len(calls) == 8  # clone, checkout x2, apply, add, commit, push, rev-parse
    for call in calls:
        argv = " ".join(call["argv"])  # type: ignore[arg-type]
        assert _CANARY not in argv
        assert encoded not in argv
        assert "extraheader" not in argv.lower()
    # Only the network commands carry the credential.
    with_auth = [c["argv"][0] for c in calls  # type: ignore[index]
                 if c["env"].get("GIT_CONFIG_VALUE_0") == header]  # type: ignore[union-attr]
    assert with_auth == ["clone", "push"]


def test_push_failure_is_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, _log, request = _setup(tmp_path, monkeypatch)
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected-by-test >&2\nexit 1\n")
    hook.chmod(0o755)
    pub = _publisher()

    with pytest.raises(GitHubError, match="push failed") as excinfo:
        pub._commit_patch(request, pub.branch_name(request.task_id), request.base_sha)
    assert _CANARY not in str(excinfo.value)


def test_git_reads_extraheader_from_env() -> None:
    """GIT_CONFIG_COUNT/KEY/VALUE are honoured by the installed git (>= 2.31),
    scoped to the clone host."""
    assert _REAL_GIT is not None
    pub = _publisher()
    env = {"PATH": "/usr/bin:/bin", **pub._git_auth_env(_HOST)}
    proc = subprocess.run(  # noqa: S603
        [_REAL_GIT, "config", "--get", f"http.https://{_HOST}/.extraheader"],
        check=True, capture_output=True, text=True, env=env,
    )
    assert proc.stdout.strip() == pub._auth_header()
    assert base64.b64decode(
        proc.stdout.strip().removeprefix("AUTHORIZATION: basic ")
    ).decode() == f"x-access-token:{_CANARY}"
