"""Shared HTTPS GET for upstream search providers (the Search Gateway side).

- https only; the destination host must be in the provider's allowlist
- the response is bounded (1 MiB) and must be a JSON object
- error messages never carry the request URL (it holds the query) or the
  request headers (they may hold an API key)
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

from agent_platform.errors import AdapterError

_MAX_RESPONSE_BYTES = 1024 * 1024


def check_endpoint(endpoint_url: str, allowed_hosts: tuple[str, ...]) -> None:
    """Refuse a non-https endpoint or a host outside the allowlist."""
    if not endpoint_url.startswith("https://"):
        msg = f"refusing non-HTTPS search endpoint: {endpoint_url}"
        raise AdapterError(msg)
    host = urllib.parse.urlsplit(endpoint_url).hostname or ""
    if host not in allowed_hosts:
        msg = f"search endpoint host {host!r} is not in the allowlist"
        raise AdapterError(msg)


def get_json(
    endpoint_url: str,
    params: Mapping[str, str],
    *,
    headers: Mapping[str, str],
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """GET endpoint_url?params and return the decoded JSON object."""
    if not endpoint_url.startswith("https://"):
        msg = f"refusing non-HTTPS search endpoint: {endpoint_url}"
        raise AdapterError(msg)
    url = f"{endpoint_url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(  # noqa: S310 — https enforced above
        url,
        headers={"Accept": "application/json",
                 "User-Agent": "agent-platform-search", **headers},
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 — https enforced above
            request, timeout=timeout_seconds
        ) as resp:
            payload = resp.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        msg = f"search provider returned {exc.code}"
        raise AdapterError(msg) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        msg = f"search provider unreachable: {type(exc).__name__}: {reason}"
        raise AdapterError(msg) from None
    if len(payload) > _MAX_RESPONSE_BYTES:
        msg = "search provider response exceeds 1 MiB"
        raise AdapterError(msg)
    try:
        data = json.loads(payload)
    except ValueError:
        msg = "search provider returned invalid JSON"
        raise AdapterError(msg) from None
    if not isinstance(data, dict):
        msg = "search provider returned a non-object JSON document"
        raise AdapterError(msg)
    return data
