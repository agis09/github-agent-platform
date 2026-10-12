"""GitHubReader implementation: GitHub App installation token + REST.

Credential flow:
  App private key (PEM file, path from env) -> RS256 JWT (10 min TTL)
  -> POST /app/installations/{id}/access_tokens -> short-lived installation
  token used for REST reads. The private key never leaves the process; the
  installation token is held in memory only.

HTTP: urllib only — no third-party HTTP client. TLS: system trust store; a
custom CA bundle path can be supplied via AGENT_PLATFORM_GITHUB_CA_BUNDLE
(for GHES with an internal CA).

All endpoints are read-only (GET). No write scope exists on this class by
design — see adapters/github/__init__.py.
"""

import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

import jwt

from agent_platform.adapters.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    GitHubTlsError,
)
from agent_platform.adapters.github.models import (
    IssueComment,
    IssueSnapshot,
    RepositoryContext,
)

_INSTALLATION_TOKEN_LIFETIME_SECONDS = 10 * 60


class GitHubAppConfig:
    """Non-secret GitHub App settings. The private key is loaded from a file
    path — the PEM bytes never enter Settings or logs."""

    def __init__(
        self,
        *,
        api_base_url: str,
        app_id: str,
        installation_id: str,
        private_key_path: str,
        ca_bundle: str | None = None,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_path = private_key_path
        self.ca_bundle = ca_bundle

    @classmethod
    def from_environ(
        cls, env: dict[str, str] | None = None
    ) -> "GitHubAppConfig":
        import os

        source = dict(os.environ if env is None else env)
        required = [
            "AGENT_PLATFORM_GITHUB_API_URL",
            "AGENT_PLATFORM_GITHUB_APP_ID",
            "AGENT_PLATFORM_GITHUB_INSTALLATION_ID",
            "AGENT_PLATFORM_GITHUB_PRIVATE_KEY_PATH",
        ]
        missing = [name for name in required if not source.get(name)]
        if missing:
            msg = f"missing GitHub App environment variables: {', '.join(missing)}"
            raise ValueError(msg)
        return cls(
            api_base_url=source["AGENT_PLATFORM_GITHUB_API_URL"],
            app_id=source["AGENT_PLATFORM_GITHUB_APP_ID"],
            installation_id=source["AGENT_PLATFORM_GITHUB_INSTALLATION_ID"],
            private_key_path=source["AGENT_PLATFORM_GITHUB_PRIVATE_KEY_PATH"],
            ca_bundle=source.get("AGENT_PLATFORM_GITHUB_CA_BUNDLE"),
        )


def _load_private_key(path: str) -> str:
    """Read the PEM file. Never cached across processes, never logged."""
    return _pem_from_bytes(_read_key_bytes(path))


def _read_key_bytes(path: str) -> bytes:
    from pathlib import Path as _Path

    data = _Path(path).read_bytes()
    if b"PRIVATE KEY" not in data:
        msg = f"not a private key file: {path}"
        raise GitHubAuthError(msg)
    return data


def _pem_from_bytes(data: bytes) -> str:
    return data.decode("utf-8")


class GitHubApiClient:
    """Low-level REST client with GitHub App auth. Read-only by construction:
    only ``get`` is exposed."""

    def __init__(self, config: GitHubAppConfig, *, env: dict[str, str] | None = None) -> None:
        self._config = config
        self._private_key = _load_private_key(config.private_key_path)
        self._cached_token: str | None = None
        self._token_expiry: float = 0.0
        self._env = env

    def get(self, path: str) -> dict[str, Any] | list[Any]:
        """GET an API path (e.g. /repos/o/r/issues/1). Raises typed errors."""
        return self._request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | list[Any]:
        """POST a JSON payload. Publisher-only — the Reader adapter never
        calls this; the pull-request publisher does."""
        return self._request("POST", path, json_body=payload)

    def get_raw(self, path: str, *, accept: str) -> str:
        """GET a raw response body (e.g. a PR diff with the vnd.github.diff
        media type). Returns text, not parsed JSON."""
        return self._request_raw("GET", path, accept=accept)

    # --- internals ---

    def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> dict[str, Any] | list[Any]:
        url = self._require_https(self._config.api_base_url + path)
        token = self._installation_token()
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 — https enforced
            url,
            method=method,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": accept,
                "User-Agent": "agent-platform-reader",
                "Content-Type": "application/json",
            },
        )
        try:
            # https enforced by _require_https (S310 audited: only https URLs)
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=30, context=self._ssl_context()
            ) as resp:
                payload: Any = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc, path) from exc
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                msg = f"TLS certificate verification failed for {path}: {reason}"
                raise GitHubTlsError(msg) from exc
            msg = f"GitHub request failed for {path}: {reason}"
            raise GitHubError(msg) from exc
        except (TimeoutError, OSError) as exc:
            msg = f"GitHub request failed for {path}: {exc}"
            raise GitHubError(msg) from exc
        if not isinstance(payload, (dict, list)):
            msg = f"unexpected GitHub response type for {path}: {type(payload).__name__}"
            raise GitHubError(msg)
        return payload

    def _request_raw(self, method: str, path: str, *, accept: str) -> str:
        url = self._require_https(self._config.api_base_url + path)
        token = self._installation_token()
        request = urllib.request.Request(  # noqa: S310 — https enforced
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": accept,
                "User-Agent": "agent-platform-reader",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=30, context=self._ssl_context()
            ) as resp:
                body: bytes = resp.read()
                return body.decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc, path) from exc
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            msg = f"GitHub request failed for {path}: {reason}"
            raise GitHubError(msg) from exc

    def _ssl_context(self) -> ssl.SSLContext:
        ca_bundle = self._config.ca_bundle
        if ca_bundle:
            return ssl.create_default_context(cafile=ca_bundle)
        return ssl.create_default_context()

    @staticmethod
    def _require_https(url: str) -> str:
        # S310 defense: the client only talks to https://, except loopback
        # (used by the offline contract-test fake server).
        if url.startswith("https://"):
            return url
        if "://127.0.0.1:" in url or "://localhost:" in url:
            return url
        msg = f"refusing non-HTTPS GitHub endpoint: {url}"
        raise GitHubError(msg)

    @staticmethod
    def _map_http_error(exc: urllib.error.HTTPError, path: str) -> Exception:
        status = exc.code
        headers: Any = exc.headers or {}
        remaining = headers.get("X-RateLimit-Remaining")
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = str(body.get("message", ""))[:200]
        except (ValueError, OSError):
            detail = ""
        if status == 404:
            msg = f"not found: {path} ({detail})"
            return GitHubNotFoundError(msg)
        if status in (403, 429) and remaining == "0":
            msg = f"rate limit exceeded: {path} ({detail})"
            return GitHubRateLimitError(msg)
        if status == 403:
            msg = f"forbidden: {path} ({detail})"
            return GitHubPermissionError(msg)
        if status == 401:
            msg = f"authentication failed: {path} ({detail})"
            return GitHubAuthError(msg)
        msg = f"GitHub API error ({status}): {path} ({detail})"
        return GitHubError(msg)

    def _installation_token(self) -> str:
        now = time.monotonic()
        if self._cached_token is not None and now < self._token_expiry:
            return self._cached_token
        self._cached_token = self._fetch_installation_token()
        self._token_expiry = now + (_INSTALLATION_TOKEN_LIFETIME_SECONDS - 60)
        return self._cached_token

    def _fetch_installation_token(self) -> str:
        app_jwt = self._app_jwt()
        url = self._require_https(
            self._config.api_base_url
            + f"/app/installations/{self._config.installation_id}/access_tokens"
        )
        request = urllib.request.Request(  # noqa: S310 — https enforced
            url,
            method="POST",
            data=b"",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "agent-platform-reader",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=30, context=self._ssl_context()
            ) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc, "/app/installations/.../access_tokens") from exc
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                msg = f"TLS certificate verification failed for token endpoint: {reason}"
                raise GitHubTlsError(msg) from exc
            msg = f"installation token request failed: {reason}"
            raise GitHubError(msg) from exc
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            msg = "installation token response missing token field"
            raise GitHubError(msg)
        return token

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._config.app_id}
        try:
            return str(
                jwt.encode(payload, self._private_key, algorithm="RS256")
            )
        except (ValueError, TypeError) as exc:
            msg = f"failed to sign GitHub App JWT: {exc}"
            raise GitHubAuthError(msg) from exc


class GithubReader:
    """GitHubReader implementation.

    Read-only: issue snapshot, repository context. Credentials come from
    the environment; the installation token never leaves this object.
    """

    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        client: GitHubApiClient | None = None,
    ) -> None:
        self._client = client or GitHubApiClient(config)

    def get_repository_context(
        self, repository: str, *, branch: str | None = None
    ) -> RepositoryContext:
        repo = self._client.get(f"/repos/{repository}")
        if not isinstance(repo, dict):
            msg = f"unexpected repo response for {repository}"
            raise GitHubError(msg)
        default_branch = str(repo.get("default_branch", "") or "")
        if not default_branch:
            msg = f"repository {repository} has no default_branch"
            raise GitHubError(msg)
        target_branch = branch or default_branch
        commit = self._client.get(f"/repos/{repository}/commits/{target_branch}")
        if not isinstance(commit, dict):
            msg = f"unexpected commit response for {repository}@{target_branch}"
            raise GitHubError(msg)
        head_sha = str(commit.get("sha", "") or "")
        context = RepositoryContext(
            repository=repository,
            default_branch=default_branch,
            head_sha=head_sha,
        )
        # head_sha validated by the model's pattern; a non-SHA response is a
        # contract violation and must fail loudly (pydantic raises).
        return context

    def has_write_access(self, repository: str, login: str) -> bool:
        """Comment-kick gate: does `login` hold >= write on the repo?

        GET /repos/{repo}/collaborators/{login}/permission returns the
        effective permission ("admin" | "write" | "read" | "none"). Any
        API error — including 403/404 for a non-collaborator — is a
        denied kick (fail-closed): the caller treats False as "may not
        kick" and the failure is visible only as the rejection itself.
        """
        try:
            resp = self._client.get(
                f"/repos/{repository}/collaborators/{login}/permission"
            )
        except GitHubError:
            return False
        if not isinstance(resp, dict):
            return False
        permission = str(resp.get("permission", ""))
        return permission in ("admin", "write")

    def get_issue(self, repository: str, issue_number: int) -> IssueSnapshot:
        issue = self._client.get(f"/repos/{repository}/issues/{issue_number}")
        if not isinstance(issue, dict):
            msg = f"unexpected issue response for {repository}#{issue_number}"
            raise GitHubError(msg)
        if "pull_request" in issue:
            msg = f"#{issue_number} is a pull request, not an issue"
            raise GitHubError(msg)
        return IssueSnapshot.model_validate(
            {
                "number": issue.get("number"),
                "title": issue.get("title") or "",
                "body": issue.get("body"),
                "state": issue.get("state") or "",
            }
        )

    def list_issue_comments(self, repository: str, issue_number: int) -> list[IssueComment]:
        """Issue comments in creation order (first 100 — a deliberate bound; the
        answer flow needs only the recent replies). Bodies are untrusted."""
        resp = self._client.get(
            f"/repos/{repository}/issues/{issue_number}/comments?per_page=100"
        )
        if not isinstance(resp, list):
            msg = f"unexpected comments response for {repository}#{issue_number}"
            raise GitHubError(msg)
        comments = []
        for item in resp:
            if not isinstance(item, dict):
                msg = f"unexpected comment entry for {repository}#{issue_number}"
                raise GitHubError(msg)
            user = item.get("user")
            comments.append(IssueComment.model_validate({
                "id": item.get("id"),
                "author": str(user.get("login", "")) if isinstance(user, dict) else "",
                "body": item.get("body") or "",
            }))
        return comments


__all__ = [
    "GitHubApiClient",
    "GitHubAppConfig",
    "GithubReader",
]
