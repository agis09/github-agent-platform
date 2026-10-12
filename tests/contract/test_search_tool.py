"""Contract: SearchTool provider switching, audit, rate limit,
and external provider allowlist."""

import datetime

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter  # noqa: F401
from agent_platform.clock import FixedClock
from agent_platform.errors import AdapterError
from agent_platform.search import (
    ExternalHttpSearchProvider,
    FakeSearchProvider,
    QueryRejectedError,
    SearchSanitizer,
    SearchTool,
)

UTC = datetime.UTC


def _tool(provider, **sanitizer_kwargs):
    clock = FixedClock(datetime.datetime(2026, 9, 16, 9, 0, 0, tzinfo=UTC))
    return SearchTool(provider, SearchSanitizer(**sanitizer_kwargs), clock=clock)


def test_fake_provider_returns_fixed_results():
    provider = FakeSearchProvider()
    tool = _tool(provider)
    result = tool.search("python subprocess timeout")
    assert result.provider == "fake"
    assert result.hits[0].title == "[fake] python documentation"


def test_switching_provider_by_config_only():
    """provider を設定変更だけで Fake/外部 に切替 — the SearchTool takes any
    SearchProvider; swapping the constructor argument is the whole switch."""
    tool_fake = _tool(FakeSearchProvider())
    assert tool_fake.search("q").provider == "fake"

    provider_ext = ExternalHttpSearchProvider(
        "https://search.example.internal.invalid/api",
        allowed_hosts=("search.example.internal.invalid",),
    )
    tool_ext = _tool(provider_ext)
    assert tool_ext._provider.name == "external-http"


def test_sanitizer_blocks_before_provider(tmp_path):
    """禁止 query は送信前に拒否される — the provider never sees it."""

    class CountingProvider(FakeSearchProvider):
        calls = 0

        def search(self, query):
            CountingProvider.calls += 1
            return super().search(query)

    tool = _tool(
        CountingProvider(),
        issue_tokens=("todos.json リセット手順",),
    )
    with pytest.raises(QueryRejectedError, match="refused before send"):
        tool.search("todos.json リセット手順 の検索")
    assert CountingProvider.calls == 0
    # The refusal is auditable.
    assert tool.audit[-1].accepted is False


def test_rate_limit_blocks_after_burst():
    tool = _tool(FakeSearchProvider(), rate_limit_per_minute=3)
    tool.search("query one")
    tool.search("query two")
    tool.search("query three")
    with pytest.raises(QueryRejectedError, match="rate limit"):
        tool.search("query four")


def test_external_provider_requires_allowlisted_host():
    with pytest.raises(AdapterError, match="not in the allowlist"):
        ExternalHttpSearchProvider(
            "https://evil.example.invalid/api",
            allowed_hosts=("search.example.invalid",),
        )


def test_external_provider_rejects_non_https():
    with pytest.raises(AdapterError, match="non-HTTPS"):
        ExternalHttpSearchProvider(
            "http://search.example.invalid/api",
            allowed_hosts=("search.example.invalid",),
        )


def test_every_accepted_query_is_audited():
    tool = _tool(FakeSearchProvider())
    tool.search("first clean query")
    tool.search("second clean query")
    assert [d.query for d in tool.audit] == ["first clean query", "second clean query"]
    assert all(d.accepted for d in tool.audit)
