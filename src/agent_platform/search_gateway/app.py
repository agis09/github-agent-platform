"""Search Gateway: the in-cluster service between
the worker's search CLI and the upstream search provider.

    worker search CLI ── POST /v1/search {query, forbidden_tokens} ──▶ gateway
    gateway: sanitizer (again) → rate limit → upstream provider → SearchResult

- The worker-side sanitizer is not trusted: every request is re-checked
  with the forbidden vocabulary it carries plus the gateway's own
  forbidden hosts.
- Rate limits: one window for the whole gateway, one per client address.
- Audit: one stderr line per request with the query's SHA-256, never the
  query text (an LLM-generated string). Whether to keep queries/results as
  artifacts is an open question.
- Upstream: one provider from the registry in search/upstreams.py (fake,
  http-json, brave, ...). The setting is required; there is no default
  provider — a deployment without search runs no gateway at all.
"""

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import ValidationError

from agent_platform.errors import AdapterError, AgentPlatformError
from agent_platform.search.base import GatewaySearchRequest, SearchProvider, SearchQuery
from agent_platform.search.sanitizer import QueryRejectedError, SearchSanitizer

_MAX_BODY_BYTES = 16 * 1024


class GatewayConfigError(AgentPlatformError):
    """The gateway settings are missing or malformed."""


@dataclass(frozen=True)
class GatewayResponse:
    status: int
    body: dict[str, object]
    log: str


@dataclass
class _WindowLimiter:
    """Sliding one-minute window per key (thread-safe)."""

    per_minute: int
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            recent = [t for t in self._hits.get(key, []) if now - t < 60]
            if len(recent) >= self.per_minute:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            return True


class SearchGateway:
    def __init__(
        self,
        provider: SearchProvider,
        *,
        forbidden_hosts: tuple[str, ...] = (),
        rate_per_minute: int = 30,
        client_rate_per_minute: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._forbidden_hosts = forbidden_hosts
        self._global = _WindowLimiter(rate_per_minute, clock)
        self._per_client = _WindowLimiter(client_rate_per_minute, clock)

    def handle_search(self, body: bytes, *, client: str) -> GatewayResponse:
        if len(body) > _MAX_BODY_BYTES:
            return self._reply(413, "invalid", client, None, reason="request too large")
        try:
            request = GatewaySearchRequest.model_validate_json(body)
        except ValidationError:
            return self._reply(400, "invalid", client, None, reason="invalid request")
        digest = hashlib.sha256(request.query.encode("utf-8")).hexdigest()
        # The sanitizer's own window is per instance; a fresh instance per
        # request leaves rate limiting to the gateway limiters below.
        sanitizer = SearchSanitizer(
            forbidden_hosts=self._forbidden_hosts,
            issue_tokens=tuple(request.forbidden_tokens),
            rate_limit_per_minute=1,
        )
        try:
            sanitizer.check(SearchQuery(query=request.query), now="")
        except QueryRejectedError as exc:
            return self._reply(403, "refused", client, digest, reason=exc.reason)
        if not self._global.allow("*") or not self._per_client.allow(client):
            return self._reply(429, "rate_limited", client, digest, reason="rate limit exceeded")
        try:
            result = self._provider.search(SearchQuery(query=request.query))
        except AdapterError:
            # The upstream error text may echo the request URL (the query);
            # neither the response nor the log carries it.
            return self._reply(502, "upstream_error", client, digest, reason="upstream failed")
        return GatewayResponse(
            status=200,
            body=result.model_dump(mode="json"),
            log=_log_line(client, "accepted", digest, hits=len(result.hits),
                          provider=result.provider),
        )

    def _reply(
        self, status: int, decision: str, client: str, digest: str | None, *, reason: str
    ) -> GatewayResponse:
        return GatewayResponse(
            status=status,
            body={"error": decision, "reason": reason},
            log=_log_line(client, decision, digest, hits=0, provider=self._provider.name,
                          reason=reason),
        )


def _log_line(
    client: str, decision: str, digest: str | None, *, hits: int, provider: str,
    reason: str = "",
) -> str:
    line = (
        f"search-gateway: client={client} decision={decision} "
        f"query_sha256={digest or '-'} hits={hits} provider={provider}"
    )
    return f"{line} reason={reason!r}" if reason else line


def provider_from_environ(env: Mapping[str, str]) -> SearchProvider:
    """The upstream named by SEARCH_GATEWAY_UPSTREAM (search/upstreams.py).
    Required: the gateway only runs when the deployment chose a provider."""
    from agent_platform.search.upstreams import (
        UpstreamConfigError,
        UpstreamSettings,
        build_upstream,
    )

    name = env.get("SEARCH_GATEWAY_UPSTREAM", "")
    if not name:
        msg = "SEARCH_GATEWAY_UPSTREAM is required (the search provider name)"
        raise GatewayConfigError(msg)
    try:
        return build_upstream(name, UpstreamSettings.from_environ(env))
    except (UpstreamConfigError, AdapterError) as exc:
        raise GatewayConfigError(str(exc)) from exc


def gateway_from_environ(env: Mapping[str, str] | None = None) -> SearchGateway:
    source = os.environ if env is None else env
    return SearchGateway(
        provider_from_environ(source),
        forbidden_hosts=tuple(
            h for h in source.get("SEARCH_FORBIDDEN_HOSTS", "").split(",") if h
        ),
        rate_per_minute=_positive_int(source, "SEARCH_GATEWAY_RATE_PER_MINUTE", 30),
        client_rate_per_minute=_positive_int(
            source, "SEARCH_GATEWAY_CLIENT_RATE_PER_MINUTE", 10
        ),
    )


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{name} must be a positive integer, got {raw!r}"
        raise GatewayConfigError(msg) from exc
    if value < 1:
        msg = f"{name} must be a positive integer, got {raw!r}"
        raise GatewayConfigError(msg)
    return value


def make_handler(gateway: SearchGateway) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — stdlib naming
            if self.path != "/v1/search":
                self._send(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, {"error": "invalid", "reason": "Content-Length required"})
                return
            if length < 0 or length > _MAX_BODY_BYTES:
                self._send(413, {"error": "invalid", "reason": "request too large"})
                return
            outcome = gateway.handle_search(
                self.rfile.read(length), client=self.client_address[0]
            )
            print(outcome.log, file=sys.stderr, flush=True)
            self._send(outcome.status, outcome.body)

        def do_GET(self) -> None:  # noqa: N802 — stdlib naming
            if self.path == "/healthz":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "not_found"})

        def _send(self, status: int, body: dict[str, object]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            # The request line is not logged; handle_search writes the audit line.
            pass

    return Handler


def serve(gateway: SearchGateway, *, port: int, bind: str = "127.0.0.1") -> None:
    server = ThreadingHTTPServer((bind, port), make_handler(gateway))
    print(f"search-gateway listening on {bind}:{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


__all__ = [
    "GatewayConfigError",
    "GatewayResponse",
    "SearchGateway",
    "gateway_from_environ",
    "make_handler",
    "provider_from_environ",
    "serve",
]
