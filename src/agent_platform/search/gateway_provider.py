"""GatewaySearchProvider: the worker-side client of the in-cluster
Search Gateway.

The worker's NetworkPolicy reaches the gateway Service and nothing else for
search; the gateway holds the upstream provider settings. The request
carries the task's forbidden vocabulary so the gateway can re-run the
sanitizer without trusting this side.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from agent_platform.errors import AdapterError
from agent_platform.search.base import GatewaySearchRequest, SearchQuery, SearchResult
from agent_platform.search.sanitizer import QueryRejectedError

_MAX_RESPONSE_BYTES = 1024 * 1024


class GatewaySearchProvider:
    """POST {query, forbidden_tokens} to <gateway>/v1/search."""

    name: str = "gateway"

    def __init__(
        self,
        gateway_url: str,
        *,
        forbidden_tokens: tuple[str, ...] = (),
        timeout_seconds: float = 30.0,
    ) -> None:
        parsed = urllib.parse.urlsplit(gateway_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            msg = f"invalid search gateway URL: {gateway_url!r}"
            raise AdapterError(msg)
        self._endpoint = gateway_url.rstrip("/") + "/v1/search"
        self._forbidden = forbidden_tokens
        self._timeout = timeout_seconds
        # In-cluster Service: never route through HTTPS_PROXY (the egress
        # proxy ACL does not list the gateway, and must not need to).
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def search(self, query: SearchQuery) -> SearchResult:
        body = GatewaySearchRequest(
            query=query.query, forbidden_tokens=list(self._forbidden)
        ).model_dump_json().encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 — scheme checked in __init__
            self._endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as resp:
                payload = resp.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            reason = _error_reason(exc)
            if exc.code == 403:
                err = QueryRejectedError(f"search query refused by gateway: {reason}")
                err.reason = reason
                raise err from exc
            msg = f"search gateway returned {exc.code}: {reason}"
            raise AdapterError(msg) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"search gateway unreachable: {exc}"
            raise AdapterError(msg) from exc
        if len(payload) > _MAX_RESPONSE_BYTES:
            msg = "search gateway response exceeds 1 MiB"
            raise AdapterError(msg)
        try:
            result = SearchResult.model_validate_json(payload)
        except ValueError as exc:
            msg = "search gateway returned an invalid SearchResult"
            raise AdapterError(msg) from exc
        if result.query != query.query:
            msg = "search gateway answered a different query"
            raise AdapterError(msg)
        return result


def _error_reason(exc: urllib.error.HTTPError) -> str:
    """The gateway's fixed-vocabulary reason, or the status text."""
    try:
        data = json.loads(exc.read(4096))
    except (ValueError, OSError):
        return str(exc.reason)
    reason = data.get("reason") if isinstance(data, dict) else None
    return str(reason)[:200] if reason else str(exc.reason)


__all__ = ["GatewaySearchProvider"]
