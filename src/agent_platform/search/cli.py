"""Worker-side search CLI: the ONLY way the agent inside the
Execute Job can reach a search provider.

Usage (called by the codex agent as a command):
    python -m agent_platform.search.cli '<query>'

Behavior:
- The query passes SearchSanitizer with the forbidden vocabulary shipped in
  EXEC_SEARCH_FORBIDDEN (JSON list, derived at plan time from the untrusted
  task context) plus the built-in rule classes (secret shapes, internal
  URLs, log fragments, code lines).
- The provider comes from SEARCH_PROVIDER: `none` (the default: search is
  disabled and every call is refused), `fake` (offline tests) or `gateway`
  (the in-cluster Search Gateway at SEARCH_GATEWAY_URL, which re-runs
  the sanitizer and holds the upstream provider the deployment selected).
  The worker never talks to an upstream search provider directly.
- Refusals exit non-zero with the reason on stderr; accepts print the
  SearchResult JSON on stdout. Both paths are auditable by the caller
  (the worker collects the decisions from the job log).
"""

import datetime
import json
import os
import sys

from agent_platform.errors import AdapterError, DomainRejectedError
from agent_platform.search.sanitizer import SearchSanitizer
from agent_platform.search.tool import SearchTool


def _provider_from_env(forbidden: tuple[str, ...]) -> "object":
    name = os.environ.get("SEARCH_PROVIDER", "none")
    if name == "none":
        msg = "search is disabled in this deployment"
        raise DomainRejectedError(msg)
    if name == "fake":
        from agent_platform.search.fake import FakeSearchProvider

        return FakeSearchProvider()
    if name == "gateway":
        from agent_platform.search.gateway_provider import GatewaySearchProvider

        url = os.environ.get("SEARCH_GATEWAY_URL", "")
        if not url:
            msg = "SEARCH_PROVIDER=gateway needs SEARCH_GATEWAY_URL"
            raise AdapterError(msg)
        return GatewaySearchProvider(url, forbidden_tokens=forbidden)
    msg = f"unknown SEARCH_PROVIDER {name!r} (expected 'none', 'fake' or 'gateway')"
    raise DomainRejectedError(msg)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m agent_platform.search.cli '<query>'", file=sys.stderr)
        return 2

    forbidden = tuple(json.loads(os.environ.get("EXEC_SEARCH_FORBIDDEN", "[]")))
    sanitizer = SearchSanitizer(
        issue_tokens=forbidden,
        forbidden_hosts=tuple(
            h for h in os.environ.get("SEARCH_FORBIDDEN_HOSTS", "").split(",") if h
        ),
    )
    try:
        tool = SearchTool(_provider_from_env(forbidden), sanitizer, clock=_Clock())
        result = tool.search(argv[0])
    except DomainRejectedError as exc:
        print(f"search refused: {exc}", file=sys.stderr)
        return 1
    except AdapterError as exc:
        print(f"search failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
    return 0


class _Clock:
    """Minimal clock for the sanitizer's audit stamps."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)


if __name__ == "__main__":
    sys.exit(main())
