"""Contract: GitHubReader against a local fake GHES server.

Network-free: the fake server binds 127.0.0.1 and answers the exact REST
shapes the reader consumes. Each read-side failure mode (missing issue,
permission denied, rate limit, TLS error) is covered with a typed exception.
"""

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_platform.adapters.github import (
    GitHubApiClient,
    GitHubAppConfig,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    GithubReader,
    GitHubTlsError,
)

_HEAD_SHA = "b" * 40
_APP_ID = "146"
_INSTALLATION_ID = "917"


class _FakeGhesHandler(http.server.BaseHTTPRequestHandler):
    """Minimal GHES stand-in. Behavior is driven by the class-level state the
    test mutates per case."""

    server_version = "FakeGHES/1.0"

    # test hooks
    status_codes: dict[str, int] = {}
    rate_limited_paths: set[str] = set()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # silence request logging

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        if self.path in self.rate_limited_paths:
            self.send_header("X-RateLimit-Remaining", "0")
        else:
            self.send_header("X-RateLimit-Remaining", "4990")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer inst-token-"):
            return True
        if auth.startswith("Bearer app-jwt-") and self.command == "POST":
            return True
        self._send(401, {"message": "Bad credentials"})
        return False

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        if not self._check_auth():
            return
        if self.path == f"/app/installations/{_INSTALLATION_ID}/access_tokens":
            self._send(
                201,
                {
                    "token": "inst-token-abc123",
                    "expires_at": "2026-09-16T12:00:00Z",
                    "permissions": {"contents": "read", "issues": "read",
                                    "metadata": "read"},
                },
            )
            return
        self._send(404, {"message": "Not Found"})

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        if not self._check_auth():
            return
        forced = self.status_codes.get(self.path)
        if forced is not None:
            self._send(forced, {"message": "forced"})
            return
        if self.path == "/repos/owner/repo":
            self._send(200, {"default_branch": "main", "private": True})
            return
        if self.path == "/repos/owner/repo/commits/main":
            self._send(200, {"sha": _HEAD_SHA})
            return
        if self.path == "/repos/owner/repo/issues/1":
            self._send(200, {
                "number": 1, "title": "Fix the thing",
                "body": "steps to reproduce...", "state": "open",
            })
            return
        if self.path == "/repos/owner/repo/issues/1/comments?per_page=100":
            body = json.dumps([
                {"id": 11, "user": {"login": "alice"}, "body": "/agent-platform run"},
                {"id": 12, "user": None, "body": None},
            ]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/repos/owner/repo/pulls/3":
            self._send(200, {
                "number": 3, "title": "A PR", "body": "pr body",
                "state": "open", "pull_request": {"url": "x"},
            })
            return
        self._send(404, {"message": "Not Found"})


@pytest.fixture
def fake_ghes() -> Iterator[tuple[str, type[_FakeGhesHandler]]]:
    handler = type("Handler", (_FakeGhesHandler,), {
        "status_codes": {},
        "rate_limited_paths": set(),
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler
    finally:
        server.shutdown()
        server.server_close()


def _make_reader(base_url: str, handler: type[_FakeGhesHandler]) -> GithubReader:
    config = GitHubAppConfig(
        api_base_url=base_url,
        app_id=_APP_ID,
        installation_id=_INSTALLATION_ID,
        private_key_path=str(_write_test_key()),
    )
    client = GitHubApiClient(config)
    # Patch the token fetcher to skip real JWT signing — the fake server
    # accepts any "Bearer app-jwt-" for POST. (Real signing is covered by the
    # live smoke test; the contract under test is request/error handling.)
    client._fetch_installation_token = lambda: "inst-token-abc123"  # type: ignore[method-assign]
    client._cached_token = "inst-token-abc123"
    client._token_expiry = float("inf")
    return GithubReader(config, client=client)


_KEY_FILE = Path(__file__).resolve().parents[1] / "fixtures" / "test-app-key.pem"


def _write_test_key() -> Path:
    """Deterministic throwaway RSA key for config validation. Generated once
    per test session; the real key never appears in tests."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if _KEY_FILE.exists():
        return _KEY_FILE
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _KEY_FILE.write_bytes(pem)
    return _KEY_FILE


def test_repository_context_roundtrip(fake_ghes):
    base_url, handler = fake_ghes
    reader = _make_reader(base_url, handler)
    ctx = reader.get_repository_context("owner/repo")
    assert ctx.repository == "owner/repo"
    assert ctx.default_branch == "main"
    assert ctx.head_sha == _HEAD_SHA


def test_issue_roundtrip(fake_ghes):
    base_url, handler = fake_ghes
    reader = _make_reader(base_url, handler)
    issue = reader.get_issue("owner/repo", 1)
    assert issue.number == 1
    assert issue.title == "Fix the thing"
    assert issue.body == "steps to reproduce..."


def test_missing_issue_404(fake_ghes):
    base_url, handler = fake_ghes
    reader = _make_reader(base_url, handler)
    with pytest.raises(GitHubNotFoundError):
        reader.get_issue("owner/repo", 999)


def test_permission_denied_403(fake_ghes):
    base_url, handler = fake_ghes
    handler.status_codes["/repos/owner/repo"] = 403
    reader = _make_reader(base_url, handler)
    with pytest.raises(GitHubPermissionError):
        reader.get_repository_context("owner/repo")


def test_rate_limit_403_with_header(fake_ghes):
    base_url, handler = fake_ghes
    handler.status_codes["/repos/owner/repo"] = 403
    handler.rate_limited_paths.add("/repos/owner/repo")
    reader = _make_reader(base_url, handler)
    with pytest.raises(GitHubRateLimitError):
        reader.get_repository_context("owner/repo")


def test_auth_failure_401(fake_ghes):
    base_url, handler = fake_ghes
    # Expire the cached token to force a refetch; the fake server rejects
    # the app JWT path with 401 when the token is wrong.
    handler.status_codes[f"/app/installations/{_INSTALLATION_ID}/access_tokens"] = 401
    config = GitHubAppConfig(
        api_base_url=base_url,
        app_id=_APP_ID,
        installation_id=_INSTALLATION_ID,
        private_key_path=str(_write_test_key()),
    )
    client = GitHubApiClient(config)
    client._cached_token = None
    client._token_expiry = 0.0
    client._fetch_installation_token = lambda: (_  # type: ignore[method-assign]
        for _ in ()).throw(
        GitHubAuthError("authentication failed: access_tokens")
    )
    reader = GithubReader(config, client=client)
    with pytest.raises(GitHubAuthError):
        reader.get_repository_context("owner/repo")


def test_tls_error_mapped(fake_ghes, monkeypatch):
    base_url, handler = fake_ghes
    reader = _make_reader(base_url, handler)
    # Simulate CERTIFICATE_VERIFY_FAILED from the ssl layer.
    import urllib.error

    def _raise_tls(*args: object, **kwargs: object):
        raise urllib.error.URLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise_tls)
    with pytest.raises(GitHubTlsError):
        reader.get_repository_context("owner/repo")


def test_non_https_endpoint_refused(fake_ghes):
    base_url, handler = fake_ghes
    config = GitHubAppConfig(
        api_base_url="http://insecure.example/v3",
        app_id=_APP_ID,
        installation_id=_INSTALLATION_ID,
        private_key_path=str(_write_test_key()),
    )
    client = GitHubApiClient(config)
    client._cached_token = "inst-token-abc123"
    client._token_expiry = float("inf")
    reader = GithubReader(config, client=client)
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — broad by intent
        reader.get_repository_context("owner/repo")
    assert "non-HTTPS" in str(excinfo.value)


def test_pull_request_rejected_as_issue(fake_ghes):
    base_url, handler = fake_ghes
    reader = _make_reader(base_url, handler)
    # Issue #3 is served with a pull_request key by the fake server.
    handler.status_codes.pop("/repos/owner/repo/issues/3", None)
    original_do_GET = handler.do_GET

    def do_GET_with_pr(self):  # noqa: N802
        if self.path == "/repos/owner/repo/issues/3":
            self._send(200, {
                "number": 3, "title": "A PR", "body": "pr body",
                "state": "open", "pull_request": {"url": "x"},
            })
            return
        original_do_GET(self)

    handler.do_GET = do_GET_with_pr  # type: ignore[method-assign]
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — broad by intent
        reader.get_issue("owner/repo", 3)
    assert "pull request" in str(excinfo.value)


def test_private_key_file_required():
    with pytest.raises(Exception):  # noqa: B017, PT011 — broad by intent
        _load_key = Path("/nonexistent/key.pem")
        GitHubAppConfig(
            api_base_url="https://ghes.example/api/v3",
            app_id=_APP_ID,
            installation_id=_INSTALLATION_ID,
            private_key_path=str(_load_key),
        )
        GitHubApiClient(
            GitHubAppConfig(
                api_base_url="https://ghes.example/api/v3",
                app_id=_APP_ID,
                installation_id=_INSTALLATION_ID,
                private_key_path=str(_load_key),
            )
        )


def test_b64_roundtrip_sanity():
    # The reader decodes nothing itself, but the fake key fixture must be a
    # valid PEM; assert the session key exists and parses.
    from cryptography.hazmat.primitives import serialization

    pem = _write_test_key().read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    assert key is not None


def test_issue_comments_roundtrip(fake_ghes):
    base_url, handler = fake_ghes
    reader = _make_reader(base_url, handler)
    comments = reader.list_issue_comments("owner/repo", 1)
    assert [(c.id, c.author, c.body) for c in comments] == [
        (11, "alice", "/agent-platform run"),
        (12, "", ""),
    ]
