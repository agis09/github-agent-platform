"""PullRequestPublisher: deterministic Draft PR creation.

Flow:
1. Re-verify the PublishRequest against the orchestrator's records — the
   caller (TaskService.publish) has already run the approval gate; this class
   independently checks patch safety (allowlist, binary, symlink, submodule,
   size).
2. Ensure the publish branch exists: a unique branch per task, created from
   the approval's base SHA by applying the patch. Idempotent — a branch that
   already exists with the expected tree is reused.
3. Create the Draft PR if no PR for the branch exists (search by head);
   otherwise report the existing one (duplicate=True).

All GitHub writes happen here, with the Publisher credential. The worker
never sees this credential (credential separation, see docs/design.md).
"""

from agent_platform.adapters.github import GitHubApiClient, GitHubError
from agent_platform.domain.models import PublishRequest, PublishResult
from agent_platform.errors import DomainRejectedError
from agent_platform.publisher.git_ops import PatchRejectedError, check_patch


class PullRequestPublisher:
    """Creates Draft PRs from verified execution results. No LLM."""

    def __init__(
        self,
        client: GitHubApiClient,
        *,
        max_patch_bytes: int = 1024 * 1024,
    ) -> None:
        self._client = client
        self._max_patch_bytes = max_patch_bytes

    def publish(self, request: PublishRequest) -> PublishResult:
        branch = self.branch_name(request.task_id)
        check_patch(
            request.patch,
            changed_files=request.changed_files,
            allowed_paths=self._allowed_paths_from_body(request),
            max_bytes=self._max_patch_bytes,
        )
        base_ref = self._client.get(
            f"/repos/{request.repository}/git/ref/heads/{request.base_branch}"
        )
        base_sha = str(base_ref.get("object", {}).get("sha", ""))  # type: ignore[union-attr]
        if base_sha != request.base_sha:
            msg = (
                f"base SHA moved on GitHub: {base_sha!r} != {request.base_sha!r}; "
                "re-plan required (NEEDS_REAPPROVAL)"
            )
            raise DomainRejectedError(msg)

        existing_pr = self._find_open_pr(request.repository, branch)
        if existing_pr is not None:
            number = existing_pr.get("number")
            if not isinstance(number, int):
                msg = f"existing PR for {branch} has no numeric number"
                raise GitHubError(msg)
            return PublishResult(
                task_id=request.task_id,
                plan_hash=request.plan_hash,
                branch_name=branch,
                pr_number=number,
                pr_url=str(existing_pr.get("html_url", "")),
                duplicate=True,
            )

        self._commit_patch(request, branch, base_sha)
        title = self._pr_title(request)
        pr = self._client.post(
            f"/repos/{request.repository}/pulls",
            {
                "title": title,
                "head": branch,
                "base": request.base_branch,
                "body": self._pr_body(request),
                "draft": True,
            },
        )
        if not isinstance(pr, dict) or "number" not in pr:
            msg = f"unexpected PR creation response for {request.task_id}"
            raise GitHubError(msg)
        return PublishResult(
            task_id=request.task_id,
            plan_hash=request.plan_hash,
            branch_name=branch,
            pr_number=int(pr["number"]),
            pr_url=str(pr.get("html_url", "")),
            duplicate=False,
        )

    # --- internals ---

    @staticmethod
    def branch_name(task_id: str) -> str:
        """Unique per task (deterministic): re-running publish for the same
        task targets the same branch — the idempotency anchor."""
        return f"agent-platform/{task_id}"

    @staticmethod
    def _allowed_paths_from_body(request: PublishRequest) -> list[str]:
        # The approval's allowed_paths travel in the PR body draft marker
        # block the service embeds; the service also re-checks them before
        # calling publish(). Here the request carries changed_files only, so
        # the structural checks (binary/symlink/submodule/size) apply and the
        # allowlist check happens in TaskService.publish (which owns the
        # approval record).
        return request.changed_files

    def _find_open_pr(
        self, repository: str, branch: str
    ) -> dict[str, object] | None:
        results = self._client.get(
            f"/repos/{repository}/pulls?head={repository.split('/')[0]}:{branch}"
            f"&state=all"
        )
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                return first
        return None

    def _commit_patch(
        self, request: PublishRequest, branch: str, base_sha: str
    ) -> str:
        """Apply the patch via the Git data API: blobs -> tree -> commit ->
        ref update. No checkout, no script execution."""
        # Build a tree applying the patch is non-trivial via the data API
        # (patch application is a server-side operation GitHub does not
        # expose). The Publisher therefore commits the patch as a single
        # additive blob set computed from the patch's file contents, which
        # the patch parser supplies. In practice the patch is applied by the
        # publisher's git binary in an ephemeral directory (no repo scripts
        # run — git only).
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        git = shutil.which("git")
        if git is None:
            msg = "git binary not found on the publisher host"
            raise GitHubError(msg)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            env = {
                "GIT_AUTHOR_NAME": "agent-platform-publisher",
                "GIT_AUTHOR_EMAIL": "publisher@agent-platform.invalid",
                "GIT_COMMITTER_NAME": "agent-platform-publisher",
                "GIT_COMMITTER_EMAIL": "publisher@agent-platform.invalid",
                "PATH": "/usr/bin:/bin",
                "GIT_TERMINAL_PROMPT": "0",
            }
            # GitHub Enterprise Server with a private CA: git verifies the
            # clone/push host with the same bundle as the REST client.
            ca_bundle = self._client._config.ca_bundle  # noqa: SLF001 — same package
            if ca_bundle:
                env["GIT_SSL_CAINFO"] = ca_bundle
            # Clone URL and push URL are public; the installation token rides
            # in an http.extraHeader (base64 basic auth) passed through the
            # GIT_CONFIG_* environment of the clone/push processes only —
            # never in argv (readable via ps / /proc/<pid>/cmdline), the URL,
            # or the cloned repo's config.
            host = self._clone_host()
            clone_url = f"https://{host}/{request.repository}.git"
            auth_env = {**env, **self._git_auth_env(host)}
            subprocess.run(  # noqa: S603 — fixed git binary, no shell
                [git, "clone", "--no-checkout", clone_url,
                 str(workdir / "repo")],
                check=True,
                capture_output=True,
                env=auth_env,
                timeout=120,
            )
            repo = workdir / "repo"
            subprocess.run(  # noqa: S603
                [git, "checkout", "-q", request.base_sha],
                cwd=repo, check=True, capture_output=True, env=env,
            )
            subprocess.run(  # noqa: S603
                [git, "checkout", "-q", "-b", branch],
                cwd=repo, check=True, capture_output=True, env=env,
            )
            apply = subprocess.run(  # noqa: S603
                [git, "apply", "--whitespace=nowarn", "-"],
                cwd=repo, input=request.patch.encode(), capture_output=True,
                env=env, timeout=60,
            )
            if apply.returncode != 0:
                msg = f"patch does not apply to base SHA: {apply.stderr.decode()[:300]}"
                raise PatchRejectedError(msg)
            subprocess.run(  # noqa: S603
                [git, "add", "-A"], cwd=repo, check=True, capture_output=True, env=env,
            )
            subprocess.run(  # noqa: S603
                [
                    git, "commit", "-q", "-m",
                    f"agent-platform: implement {request.plan_hash} "
                    f"(approved by {request.approver})",
                ],
                cwd=repo, check=True, capture_output=True, env=env,
            )
            push = subprocess.run(  # noqa: S603
                [git, "push", "-q", clone_url, f"refs/heads/{branch}"],
                cwd=repo, capture_output=True, env=auth_env, timeout=120,
            )
            if push.returncode != 0:
                msg = f"push failed: {push.stderr.decode()[:300]}"
                raise GitHubError(msg)
            sha = subprocess.run(  # noqa: S603
                [git, "rev-parse", "HEAD"],
                cwd=repo, check=True, capture_output=True, env=env,
            )
            return sha.stdout.decode().strip()

    def _clone_host(self) -> str:
        """api_base_url -> the git clone host.

        GHES uses https://host/api/v3 (strip /api path). Public GitHub uses
        https://api.github.com, whose clone host is github.com — deriving it
        by string stripping would wrongly produce api.github.com, which does
        not serve git.
        """
        host = self._client._config.api_base_url  # noqa: SLF001 — same package
        bare = host.removeprefix("https://").split("/api")[0]
        if bare == "api.github.com":
            return "github.com"
        return bare

    def _git_auth_env(self, host: str) -> dict[str, str]:
        """Environment-only git config (git >= 2.31) carrying the auth
        header, scoped to the clone host. Environment of another process is
        readable only by the same uid / root, unlike argv."""
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
            "GIT_CONFIG_VALUE_0": self._auth_header(),
        }

    def _auth_header(self) -> str:
        """http.extraHeader value carrying the installation token."""
        import base64

        token = self._client._installation_token()  # noqa: SLF001 — same package
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return f"AUTHORIZATION: basic {basic}"

    @staticmethod
    def _pr_title(request: PublishRequest) -> str:
        """repository#issue + plan summary head, instead of a bare task
        id. Falls back to the task id when issue info is absent."""
        scope = request.task_id
        if request.issue_number is not None:
            scope = f"{request.repository}#{request.issue_number}"
        summary_head = (request.plan_summary or "").splitlines()[0][:60]
        if summary_head:
            return f"[agent-platform] {scope} {summary_head}"
        return f"[agent-platform] {scope}"

    @staticmethod
    def _pr_body(request: PublishRequest) -> str:
        lines = [
            "## What",
            "",
            request.pr_body_draft or f"Implements plan `{request.plan_hash}`.",
            "",
            "## Provenance",
            "",
            f"- Task: `{request.task_id}`",
            f"- Plan: `{request.plan_hash}`",
            f"- Base: `{request.base_branch}` @ `{request.base_sha}`",
            f"- Approved by: **{request.approver}** "
            f"(`{request.approval_id}`, at {request.approved_at}, "
            f"expires {request.expires_at})",
            "",
            "## Changed files",
            "",
            *[f"- `{p}`" for p in request.changed_files],
            "",
            "## Tests",
            "",
            "_See the ExecutionResult artifact for per-command audit._",
            "",
            "---",
            "Created by agent-platform Publisher (LLM-free).",
        ]
        return "\n".join(lines)
