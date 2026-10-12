"""BraveSearchProvider: Brave Search API (web search) as a gateway upstream.

    GET https://api.search.brave.com/res/v1/web/search?q=<query>&count=<n>
    X-Subscription-Token: <api key>
    -> {"web": {"results": [{"title", "url", "description", ...}]}}

The descriptions carry HTML highlight markup (<strong>) and entities; they
are reduced to plain text. A response without a "web" section means no web
results (Brave omits it), not an error.
"""

import html
import re

from agent_platform.errors import AdapterError
from agent_platform.search.base import SearchHit, SearchQuery, SearchResult
from agent_platform.search.http import check_endpoint, get_json

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_HOSTS = ("api.search.brave.com",)
_TAG = re.compile(r"<[^>]{0,200}>")


class BraveSearchProvider:
    name: str = "brave"

    def __init__(self, api_key: str, *, count: int = 5) -> None:
        if not api_key:
            msg = "the brave upstream needs an API key"
            raise AdapterError(msg)
        if not 1 <= count <= 20:  # the API's own bound
            msg = f"brave result count must be 1..20, got {count}"
            raise AdapterError(msg)
        check_endpoint(BRAVE_ENDPOINT, _BRAVE_HOSTS)
        self._headers = {"X-Subscription-Token": api_key}
        self._count = count

    def search(self, query: SearchQuery) -> SearchResult:
        payload = get_json(
            BRAVE_ENDPOINT,
            {"q": query.query, "count": str(self._count)},
            headers=self._headers,
        )
        web = payload.get("web")
        results = web.get("results", []) if isinstance(web, dict) else []
        hits = [
            SearchHit(
                title=_plain(item.get("title"))[:512],
                url=str(item.get("url", ""))[:2048],
                snippet=_plain(item.get("description"))[:2048],
            )
            for item in (results if isinstance(results, list) else [])
            if isinstance(item, dict)
        ]
        return SearchResult(query=query.query, hits=hits, provider=self.name)


def _plain(value: object) -> str:
    return html.unescape(_TAG.sub("", str(value or ""))).strip()


__all__ = ["BRAVE_ENDPOINT", "BraveSearchProvider"]
