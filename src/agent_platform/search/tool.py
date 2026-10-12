"""SearchTool: the ONLY search entry point the
Worker can reach. Wraps a provider behind the sanitizer.

- FakeSearchProvider for offline/test runs
- ExternalHttpSearchProvider: the generic "http-json" upstream (the other
  upstreams and the registry live in search/upstreams.py)
"""

from agent_platform.clock import Clock
from agent_platform.search.base import SearchHit, SearchQuery, SearchResult
from agent_platform.search.http import check_endpoint, get_json
from agent_platform.search.sanitizer import SanitizerDecision, SearchSanitizer


class SearchTool:
    """Sanitized search gateway. Every query is sanitized first; only
    accepted queries reach the provider."""

    def __init__(
        self,
        provider: "object",
        sanitizer: SearchSanitizer,
        *,
        clock: Clock,
    ) -> None:
        from agent_platform.search.base import SearchProvider

        self._provider: SearchProvider = provider  # type: ignore[assignment]
        self._sanitizer = sanitizer
        self._clock = clock
        self.audit: list[SanitizerDecision] = []

    def search(self, query: str) -> SearchResult:
        sq = SearchQuery(query=query)
        try:
            decision = self._sanitizer.check(sq, now=self._clock_now())
        except Exception:
            # Refusals are auditable too: record them before propagating.
            self.audit.append(
                SanitizerDecision(
                    query=query, accepted=False,
                    reason="refused (see sanitizer rules)",
                    at=self._clock_now(),
                )
            )
            raise
        self.audit.append(decision)
        return self._provider.search(sq)

    def _clock_now(self) -> str:
        return self._clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


class ExternalHttpSearchProvider:
    """Generic JSON search API ("http-json" upstream): for a self-hosted or
    in-house search service that speaks this contract:

        GET <endpoint>?q=<query>
        -> {"results": [{"title": ..., "url": ..., "snippet": ...}]}

    An optional API key is sent as `Authorization: Bearer <key>`. The
    destination host must be in the configured allowlist."""

    name: str = "external-http"

    def __init__(
        self,
        endpoint_url: str,
        *,
        allowed_hosts: tuple[str, ...],
        api_key: str = "",
    ) -> None:
        check_endpoint(endpoint_url, allowed_hosts)
        self._endpoint = endpoint_url
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def search(self, query: SearchQuery) -> SearchResult:
        payload = get_json(self._endpoint, {"q": query.query}, headers=self._headers)
        results = payload.get("results", [])
        hits = [
            SearchHit(
                title=str(item.get("title", ""))[:512],
                url=str(item.get("url", ""))[:2048],
                snippet=str(item.get("snippet", ""))[:2048],
            )
            for item in (results if isinstance(results, list) else [])
            if isinstance(item, dict)
        ]
        return SearchResult(query=query.query, hits=hits, provider=self.name)
