"""Clone-host derivation for the Publisher (public GitHub support).

The Publisher derives the git clone/push host from the GitHub API base URL.
GHES uses ``https://host/api/v3``; public GitHub uses ``https://api.github.com``
whose git host is ``github.com``. Deriving by string stripping alone would
produce ``api.github.com`` as the clone host, which does not serve git.
"""

import base64
from pathlib import Path

from agent_platform.adapters.github import GitHubApiClient
from agent_platform.adapters.github.reader import GitHubAppConfig
from agent_platform.publisher.publisher import PullRequestPublisher

_KEY_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "test-app-key.pem"


def _publisher_for(api_base_url: str) -> PullRequestPublisher:
    config = GitHubAppConfig(
        api_base_url=api_base_url,
        app_id=1,
        installation_id=1,
        private_key_path=str(_KEY_PATH),
    )
    client = GitHubApiClient(config)
    # Token fetch must not run here; only the clone host is under test.
    client._fetch_installation_token = lambda: "test-token"  # type: ignore[method-assign]
    return PullRequestPublisher(client)


def test_clone_host_ghes() -> None:
    pub = _publisher_for("https://ghe.example.com/api/v3")
    assert pub._clone_host() == "ghe.example.com"


def test_clone_host_public_github() -> None:
    pub = _publisher_for("https://api.github.com")
    assert pub._clone_host() == "github.com"


def test_clone_host_ghe_subdomain_without_api_path() -> None:
    pub = _publisher_for("https://ghe.example.com")
    assert pub._clone_host() == "ghe.example.com"


def test_auth_header_format_unchanged() -> None:
    """Token still rides in http.extraHeader (base64 basic), never in argv."""
    pub = _publisher_for("https://api.github.com")
    header = pub._auth_header()
    assert header.startswith("AUTHORIZATION: basic ")
    encoded = header.removeprefix("AUTHORIZATION: basic ")
    decoded = base64.b64decode(encoded).decode()
    assert decoded.startswith("x-access-token:test-token")
