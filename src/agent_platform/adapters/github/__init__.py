"""GitHub read adapter.

Read boundary only: Issue / repository metadata via GitHub App installation
tokens. The write side (PR Publisher) is a separate adapter — the
read and write credentials must never share a type or process
(credential separation, see docs/design.md).
"""

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
from agent_platform.adapters.github.reader import (
    GitHubApiClient,
    GitHubAppConfig,
    GithubReader,
)

__all__ = [
    "GitHubApiClient",
    "GitHubAppConfig",
    "GitHubAuthError",
    "GitHubError",
    "GitHubNotFoundError",
    "GitHubPermissionError",
    "GitHubRateLimitError",
    "GitHubTlsError",
    "GithubReader",
    "IssueComment",
    "IssueSnapshot",
    "RepositoryContext",
]
