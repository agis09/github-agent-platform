"""FakeSearchProvider: deterministic, network-free."""

from agent_platform.search.base import SearchHit, SearchQuery, SearchResult


class FakeSearchProvider:
    """Returns a fixed response for any query. Tests and offline runs."""

    name: str = "fake"

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self._hits = hits or [
            SearchHit(
                title="[fake] python documentation",
                url="https://docs.example.invalid/python",
                snippet="Deterministic search result for offline testing.",
            )
        ]

    def search(self, query: SearchQuery) -> SearchResult:
        return SearchResult(query=query.query, hits=list(self._hits),
                            provider=self.name)
