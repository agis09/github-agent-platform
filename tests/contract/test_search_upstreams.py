"""Search upstream registry + Brave provider (network-free).

- the gateway builds exactly the provider the deployment named; unknown
  names and settings the provider does not use are refused (no silent
  fallback)
- Brave: request shape (endpoint, query, count, subscription header),
  response parsing (HTML stripped), errors without the key or the query
- http-json (your own API): optional Bearer key
- the execute prompt advertises search only when search is enabled
"""

import io
import json
import urllib.error

import pytest

from agent_platform.errors import AdapterError
from agent_platform.search.base import SearchQuery
from agent_platform.search.brave import BRAVE_ENDPOINT, BraveSearchProvider
from agent_platform.search.upstreams import (
    UPSTREAMS,
    UpstreamConfigError,
    UpstreamSettings,
    build_upstream,
)
from agent_platform.search_gateway import GatewayConfigError, gateway_from_environ

_KEY = "canary-brave-key-5d1e"


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def http(monkeypatch):
    """Capture the outgoing request; serve a canned JSON body."""
    calls: list = []
    state = {"body": b"{}", "error": None}

    def fake_urlopen(request, timeout):
        calls.append(request)
        if state["error"] is not None:
            raise state["error"]
        return _FakeResponse(state["body"])

    monkeypatch.setattr("agent_platform.search.http.urllib.request.urlopen", fake_urlopen)
    return calls, state


# --- registry ---


def test_registry_lists_the_shipped_providers():
    assert {"fake", "brave", "http-json"} <= set(UPSTREAMS)


def test_unknown_provider_is_refused_with_the_known_names():
    with pytest.raises(UpstreamConfigError, match=r"unknown search upstream 'bing'.*brave"):
        build_upstream("bing", UpstreamSettings())


def test_brave_needs_an_api_key():
    with pytest.raises(UpstreamConfigError, match="SEARCH_GATEWAY_API_KEY"):
        build_upstream("brave", UpstreamSettings())


def test_unused_url_is_refused_not_ignored():
    with pytest.raises(UpstreamConfigError, match="takes no SEARCH_GATEWAY_UPSTREAM_URL"):
        build_upstream("brave", UpstreamSettings(url="https://x.invalid/s", api_key=_KEY))


def test_unused_api_key_is_tolerated():
    # terraform.env exports the key whatever the provider; fake ignores it.
    assert build_upstream("fake", UpstreamSettings(api_key=_KEY)).name == "fake"


def test_http_json_needs_an_https_url():
    with pytest.raises(UpstreamConfigError, match="https://"):
        build_upstream("http-json", UpstreamSettings(url="http://search.invalid/api"))


def test_a_new_provider_is_one_registry_entry(monkeypatch):
    from agent_platform.search.fake import FakeSearchProvider
    from agent_platform.search.upstreams import Upstream

    class InHouse(FakeSearchProvider):
        name = "in-house"

    monkeypatch.setitem(UPSTREAMS, "in-house", Upstream(lambda s: InHouse()))
    gateway = gateway_from_environ({"SEARCH_GATEWAY_UPSTREAM": "in-house"})
    outcome = gateway.handle_search(b'{"query": "python asyncio"}', client="10.42.0.9")
    assert outcome.status == 200
    assert outcome.body["provider"] == "in-house"


# --- gateway configuration ---


def test_gateway_builds_brave_from_env():
    gateway = gateway_from_environ({
        "SEARCH_GATEWAY_UPSTREAM": "brave", "SEARCH_GATEWAY_API_KEY": _KEY,
    })
    assert gateway._provider.name == "brave"


def test_gateway_config_errors_name_the_problem():
    with pytest.raises(GatewayConfigError, match="needs SEARCH_GATEWAY_API_KEY"):
        gateway_from_environ({"SEARCH_GATEWAY_UPSTREAM": "brave"})
    with pytest.raises(GatewayConfigError, match="unknown search upstream"):
        gateway_from_environ({"SEARCH_GATEWAY_UPSTREAM": "none"})


# --- Brave provider ---


def test_brave_request_shape(http):
    calls, state = http
    state["body"] = json.dumps({"web": {"results": []}}).encode()
    BraveSearchProvider(_KEY).search(SearchQuery(query="python asyncio timeout"))
    (request,) = calls
    assert request.full_url == f"{BRAVE_ENDPOINT}?q=python+asyncio+timeout&count=5"
    assert request.get_header("X-subscription-token") == _KEY
    assert request.get_header("Accept") == "application/json"
    # The key travels only in the header, never in the URL.
    assert _KEY not in request.full_url


def test_brave_results_become_plain_text_hits(http):
    _, state = http
    state["body"] = json.dumps({"web": {"results": [
        {"title": "asyncio &mdash; <strong>Python</strong> docs",
         "url": "https://docs.python.org/3/library/asyncio.html",
         "description": "Use <strong>asyncio</strong>.wait_for() &amp; timeouts",
         "extra_snippets": ["ignored"]},
        "not-an-object",
    ]}}).encode()
    result = BraveSearchProvider(_KEY).search(SearchQuery(query="python asyncio"))
    assert result.provider == "brave"
    assert result.query == "python asyncio"
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.title == "asyncio — Python docs"
    assert hit.url == "https://docs.python.org/3/library/asyncio.html"
    assert hit.snippet == "Use asyncio.wait_for() & timeouts"


def test_brave_without_web_section_is_no_hits(http):
    _, state = http
    state["body"] = json.dumps({"type": "search", "query": {"original": "x"}}).encode()
    assert BraveSearchProvider(_KEY).search(SearchQuery(query="zzzz")).hits == []


def test_brave_http_error_hides_key_and_query(http):
    _, state = http
    state["error"] = urllib.error.HTTPError(
        f"{BRAVE_ENDPOINT}?q=secret+query", 401, "Unauthorized",
        {"X-Subscription-Token": _KEY}, None,  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterError) as excinfo:
        BraveSearchProvider(_KEY).search(SearchQuery(query="secret query"))
    assert str(excinfo.value) == "search provider returned 401"
    assert excinfo.value.__cause__ is None  # the URL-bearing error is not chained


def test_brave_rejects_oversized_and_non_object_responses(http):
    _, state = http
    state["body"] = b"[1, 2]"
    with pytest.raises(AdapterError, match="non-object"):
        BraveSearchProvider(_KEY).search(SearchQuery(query="q"))
    state["body"] = b" " * (1024 * 1024 + 1)
    with pytest.raises(AdapterError, match="exceeds 1 MiB"):
        BraveSearchProvider(_KEY).search(SearchQuery(query="q"))


def test_brave_needs_a_key_at_construction():
    with pytest.raises(AdapterError, match="API key"):
        BraveSearchProvider("")


# --- http-json (your own search API) ---


def test_http_json_sends_the_key_as_bearer(http):
    calls, state = http
    state["body"] = json.dumps({"results": [
        {"title": "t", "url": "https://u.invalid", "snippet": "s"},
    ]}).encode()
    provider = build_upstream("http-json", UpstreamSettings(
        url="https://search.example.invalid/api", api_key=_KEY,
    ))
    result = provider.search(SearchQuery(query="python asyncio"))
    assert [h.title for h in result.hits] == ["t"]
    (request,) = calls
    assert request.full_url == "https://search.example.invalid/api?q=python+asyncio"
    assert request.get_header("Authorization") == f"Bearer {_KEY}"


def test_http_json_without_key_sends_no_authorization(http):
    calls, state = http
    state["body"] = b'{"results": []}'
    build_upstream("http-json", UpstreamSettings(
        url="https://search.example.invalid/api",
    )).search(SearchQuery(query="q"))
    assert calls[0].get_header("Authorization") is None


# --- the execute prompt mentions search only when it is enabled ---


def _prompt(tmp_path, monkeypatch, *, gateway_url):
    from agent_platform import cli
    from agent_platform.domain.models import ExecuteJobSpec

    digest = "ab" + "0" * 62
    plan_file = tmp_path / "artifacts" / "ab" / "00" / digest
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("{}")
    monkeypatch.setenv("AGENT_PLATFORM_HOME", str(tmp_path))
    if gateway_url:
        monkeypatch.setenv("AGENT_PLATFORM_SEARCH_GATEWAY_URL", gateway_url)
    else:
        monkeypatch.delenv("AGENT_PLATFORM_SEARCH_GATEWAY_URL", raising=False)
    return cli._execute_prompt(ExecuteJobSpec(
        task_id="task-0123456789ab", repository="owner/repo",
        plan_hash="sha256:" + "a" * 64, base_branch="main", base_sha="c" * 40,
        allowed_paths=["a.py"], test_commands=[], search_forbidden_tokens=["a.py"],
        plan_ref=f"artifact://{digest}",
    ))


def test_prompt_has_no_search_when_disabled(tmp_path, monkeypatch):
    assert "agent_platform.search.cli" not in _prompt(tmp_path, monkeypatch, gateway_url="")


def test_prompt_offers_search_when_enabled(tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch,
                     gateway_url="http://search-gateway.agent-platform.svc.cluster.local:8080")
    assert "python -m agent_platform.search.cli '<query>'" in prompt
