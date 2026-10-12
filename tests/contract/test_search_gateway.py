"""Contract: Search Gateway + GatewaySearchProvider (offline — the gateway
runs in-process on 127.0.0.1 with the fake upstream).

- fake and gateway providers satisfy the same SearchProvider contract
- the gateway re-runs the sanitizer (a request that skipped the worker-side
  check is still refused) and rate-limits per client
- the gateway log carries the query's SHA-256, never the query text
"""

import hashlib
import json
import threading
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from agent_platform.errors import AdapterError
from agent_platform.search.base import SearchQuery, SearchResult
from agent_platform.search.fake import FakeSearchProvider
from agent_platform.search.gateway_provider import GatewaySearchProvider
from agent_platform.search.sanitizer import QueryRejectedError
from agent_platform.search_gateway import (
    GatewayConfigError,
    SearchGateway,
    gateway_from_environ,
    make_handler,
)


class _Server:
    def __init__(self, gateway: SearchGateway) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gateway))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_Server":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def gateway_url() -> Iterator[str]:
    with _Server(SearchGateway(FakeSearchProvider())) as server:
        yield server.url


def _post(url: str, body: bytes) -> tuple[int, dict]:
    request = urllib.request.Request(  # noqa: S310 — local test server
        url + "/v1/search", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# --- the SearchProvider contract, for both providers ---

@pytest.mark.parametrize("kind", ["fake", "gateway"])
def test_providers_satisfy_the_same_contract(kind, gateway_url):
    provider = (
        FakeSearchProvider() if kind == "fake" else GatewaySearchProvider(gateway_url)
    )
    result = provider.search(SearchQuery(query="python subprocess timeout"))
    assert isinstance(result, SearchResult)
    assert result.query == "python subprocess timeout"
    assert [h.title for h in result.hits] == ["[fake] python documentation"]
    # The gateway passes the upstream's identity through for audit.
    assert result.provider == "fake"
    assert isinstance(provider.name, str)


# --- gateway-side enforcement ---

def test_gateway_refuses_forbidden_tokens_without_trusting_the_worker(gateway_url):
    """A raw request (no worker-side sanitizer) carrying the task's
    forbidden vocabulary is still refused, and the reason does not echo
    the query."""
    status, body = _post(gateway_url, json.dumps({
        "query": "todos.json reset procedure",
        "forbidden_tokens": ["todos.json"],
    }).encode())
    assert status == 403
    assert body == {"error": "refused",
                    "reason": "query contains task context (issue/code) text"}


def test_gateway_provider_maps_refusal_to_query_rejected(gateway_url):
    provider = GatewaySearchProvider(gateway_url, forbidden_tokens=("todos.json",))
    with pytest.raises(QueryRejectedError, match="refused by gateway") as info:
        provider.search(SearchQuery(query="todos.json reset procedure"))
    assert info.value.reason == "query contains task context (issue/code) text"


@pytest.mark.parametrize("body", [
    b"not json",
    b'{"query": ""}',
    b'{"query": "q", "unexpected": 1}',
    json.dumps({"query": "q", "forbidden_tokens": ["t"] * 257}).encode(),
])
def test_gateway_rejects_invalid_requests(gateway_url, body):
    status, payload = _post(gateway_url, body)
    assert status == 400
    assert payload == {"error": "invalid", "reason": "invalid request"}


def test_gateway_rejects_oversized_body():
    gateway = SearchGateway(FakeSearchProvider())
    outcome = gateway.handle_search(b"x" * (16 * 1024 + 1), client="10.42.0.9")
    assert outcome.status == 413


def test_gateway_rate_limits_per_client():
    now = [0.0]
    gateway = SearchGateway(FakeSearchProvider(), rate_per_minute=100,
                            client_rate_per_minute=2, clock=lambda: now[0])
    body = b'{"query": "python asyncio"}'
    assert gateway.handle_search(body, client="10.42.0.9").status == 200
    assert gateway.handle_search(body, client="10.42.0.9").status == 200
    limited = gateway.handle_search(body, client="10.42.0.9")
    assert limited.status == 429
    assert limited.body == {"error": "rate_limited", "reason": "rate limit exceeded"}
    # Another client has its own window; the first recovers after a minute.
    assert gateway.handle_search(body, client="10.42.0.10").status == 200
    now[0] = 61.0
    assert gateway.handle_search(body, client="10.42.0.9").status == 200


def test_gateway_rate_limits_globally():
    gateway = SearchGateway(FakeSearchProvider(), rate_per_minute=1,
                            client_rate_per_minute=10, clock=lambda: 0.0)
    body = b'{"query": "python asyncio"}'
    assert gateway.handle_search(body, client="10.42.0.9").status == 200
    assert gateway.handle_search(body, client="10.42.0.10").status == 429


def test_gateway_hides_upstream_errors():
    class FailingProvider:
        name = "external-http"

        def search(self, query):
            raise AdapterError(f"search provider unreachable: GET ?q={query.query}")

    outcome = SearchGateway(FailingProvider()).handle_search(
        b'{"query": "python asyncio"}', client="10.42.0.9"
    )
    assert outcome.status == 502
    assert outcome.body == {"error": "upstream_error", "reason": "upstream failed"}
    assert "python asyncio" not in outcome.log


def test_gateway_provider_surfaces_upstream_failure(tmp_path):
    class FailingProvider:
        name = "external-http"

        def search(self, query):
            raise AdapterError("boom")

    with _Server(SearchGateway(FailingProvider())) as server:  # type: ignore[arg-type]
        with pytest.raises(AdapterError, match="returned 502: upstream failed"):
            GatewaySearchProvider(server.url).search(SearchQuery(query="python asyncio"))


def test_gateway_provider_unreachable_is_an_adapter_error():
    with pytest.raises(AdapterError, match="unreachable"):
        GatewaySearchProvider("http://127.0.0.1:9", timeout_seconds=2).search(
            SearchQuery(query="python asyncio")
        )


def test_gateway_provider_rejects_bad_urls():
    with pytest.raises(AdapterError, match="invalid search gateway URL"):
        GatewaySearchProvider("ftp://gateway")


# --- audit log: digest only (canary) ---

def test_gateway_log_carries_the_digest_not_the_query():
    canary = "ghp_" + "Z" * 36
    gateway = SearchGateway(FakeSearchProvider())
    accepted = gateway.handle_search(b'{"query": "python asyncio timeout"}',
                                     client="10.42.0.9")
    refused = gateway.handle_search(
        json.dumps({"query": f"why does {canary} fail"}).encode(), client="10.42.0.9"
    )
    assert refused.status == 403
    assert accepted.log == (
        "search-gateway: client=10.42.0.9 decision=accepted query_sha256="
        + hashlib.sha256(b"python asyncio timeout").hexdigest()
        + " hits=1 provider=fake"
    )
    for outcome in (accepted, refused):
        assert "python asyncio" not in outcome.log
        assert canary not in outcome.log
        assert canary not in json.dumps(outcome.body)


# --- configuration ---

def test_gateway_needs_an_explicit_upstream():
    with pytest.raises(GatewayConfigError, match="SEARCH_GATEWAY_UPSTREAM"):
        gateway_from_environ({})


def test_http_json_upstream_needs_url_and_hosts():
    with pytest.raises(GatewayConfigError, match="UPSTREAM_URL"):
        gateway_from_environ({"SEARCH_GATEWAY_UPSTREAM": "http-json"})


@pytest.mark.parametrize("value", ["0", "-1", "ten"])
def test_rate_settings_must_be_positive_integers(value):
    with pytest.raises(GatewayConfigError, match="positive integer"):
        gateway_from_environ({"SEARCH_GATEWAY_UPSTREAM": "fake",
                              "SEARCH_GATEWAY_RATE_PER_MINUTE": value})


# --- the worker CLI end to end through the gateway ---

def test_search_cli_uses_the_gateway(gateway_url, monkeypatch, capsys):
    from agent_platform.search import cli

    monkeypatch.setenv("SEARCH_PROVIDER", "gateway")
    monkeypatch.setenv("SEARCH_GATEWAY_URL", gateway_url)
    monkeypatch.setenv("EXEC_SEARCH_FORBIDDEN", json.dumps(["todos.json"]))
    assert cli.main(["python asyncio timeout"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["query"] == "python asyncio timeout"
    assert out["provider"] == "fake"


def test_search_cli_gateway_needs_a_url(monkeypatch, capsys):
    from agent_platform.search import cli

    monkeypatch.setenv("SEARCH_PROVIDER", "gateway")
    monkeypatch.delenv("SEARCH_GATEWAY_URL", raising=False)
    assert cli.main(["python asyncio"]) == 1
    assert "needs SEARCH_GATEWAY_URL" in capsys.readouterr().err
