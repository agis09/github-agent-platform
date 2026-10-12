"""GitHub adapter errors.

Each read-side failure mode maps to a typed exception:
missing issue, insufficient permissions, rate limit, TLS errors.
"""

from agent_platform.errors import AdapterError, NotFoundError


class GitHubError(AdapterError):
    """GitHub read failed. Exit code 1 (adapter failure)."""


class GitHubNotFoundError(NotFoundError):
    """Repository or issue does not exist (HTTP 404). Exit code 4."""


class GitHubPermissionError(GitHubError):
    """Token lacks the required permission (HTTP 403 without rate-limit
    headers)."""


class GitHubRateLimitError(GitHubError):
    """Rate limit exhausted (HTTP 403/429 with rate-limit headers)."""


class GitHubTlsError(GitHubError):
    """TLS certificate validation failed (self-signed / private CA)."""


class GitHubAuthError(GitHubError):
    """GitHub App JWT or installation token exchange failed (HTTP 401)."""
