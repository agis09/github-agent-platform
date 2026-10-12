"""Search provider protocol and data contracts.

The Worker never gets raw Internet access; it calls a SearchTool whose
provider is swappable by configuration (Fake / external / internal gateway).
Every query passes through the sanitizer before any provider sees it.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class SearchQuery(BaseModel):
    """A search request. Only the (sanitized) query string travels out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=512)


class SearchHit(BaseModel):
    """One result returned by a provider. URLs are recorded for audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(max_length=512)
    url: str = Field(max_length=2048)
    snippet: str = Field(max_length=2048)


class SearchResult(BaseModel):
    """Provider response. The query echoes back for audit correlation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    hits: list[SearchHit] = []
    provider: str


class GatewaySearchRequest(BaseModel):
    """Search Gateway request body (worker -> gateway). The gateway
    re-runs the sanitizer with forbidden_tokens; it does not trust the
    worker-side check. Bounds keep one request small."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=512)
    forbidden_tokens: list[str] = Field(default=[], max_length=256)


class SearchProvider(Protocol):
    name: str

    def search(self, query: SearchQuery) -> SearchResult: ...
