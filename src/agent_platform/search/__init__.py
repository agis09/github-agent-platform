"""Search adapter package.

Fake / external / in-cluster gateway providers behind one
sanitized SearchTool. The Worker has no direct Internet egress; the
NetworkPolicy for sandbox pods stays unchanged.
"""

from agent_platform.search.base import SearchHit, SearchProvider, SearchQuery, SearchResult
from agent_platform.search.fake import FakeSearchProvider
from agent_platform.search.sanitizer import (
    QueryRejectedError,
    SanitizerDecision,
    SearchSanitizer,
)
from agent_platform.search.tool import ExternalHttpSearchProvider, SearchTool

__all__ = [
    "ExternalHttpSearchProvider",
    "FakeSearchProvider",
    "QueryRejectedError",
    "SanitizerDecision",
    "SearchHit",
    "SearchProvider",
    "SearchQuery",
    "SearchResult",
    "SearchSanitizer",
    "SearchTool",
]
