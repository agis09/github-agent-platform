"""Registry of upstream search providers the Search Gateway can call.

The deployment selects exactly one by name (SEARCH_GATEWAY_UPSTREAM, set
from terraform's `search_provider`; `none` there means no gateway at all,
so search is disabled). Each entry builds a SearchProvider from the shared
settings below:

    SEARCH_GATEWAY_UPSTREAM_URL  endpoint, for upstreams that take one
    SEARCH_GATEWAY_API_KEY       credential (from a k8s Secret), if needed

Adding a search API = a class implementing SearchProvider (see brave.py;
search/http.py has the bounded https GET) + one entry in UPSTREAMS. The
gateway, the worker CLI and the NetworkPolicies need no change: every
non-fake upstream gets the same 443 egress, and each provider pins its
own destination host.

A URL the selected upstream does not use is an error, not ignored — it
means the configuration targets a different provider. An unused API key is
tolerated: terraform.env exports it for whichever provider is selected.
"""

import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from agent_platform.errors import AgentPlatformError
from agent_platform.search.base import SearchProvider


class UpstreamConfigError(AgentPlatformError):
    """The selected upstream is unknown or its settings are incomplete."""


@dataclass(frozen=True)
class UpstreamSettings:
    url: str = ""
    api_key: str = ""

    @classmethod
    def from_environ(cls, env: Mapping[str, str]) -> "UpstreamSettings":
        return cls(
            url=env.get("SEARCH_GATEWAY_UPSTREAM_URL", ""),
            api_key=env.get("SEARCH_GATEWAY_API_KEY", ""),
        )


@dataclass(frozen=True)
class Upstream:
    build: Callable[[UpstreamSettings], SearchProvider]
    uses_url: bool = False


def _fake(settings: UpstreamSettings) -> SearchProvider:
    from agent_platform.search.fake import FakeSearchProvider

    return FakeSearchProvider()


def _http_json(settings: UpstreamSettings) -> SearchProvider:
    from agent_platform.search.tool import ExternalHttpSearchProvider

    if not settings.url.startswith("https://"):
        msg = "the http-json upstream needs an https:// SEARCH_GATEWAY_UPSTREAM_URL"
        raise UpstreamConfigError(msg)
    host = urllib.parse.urlsplit(settings.url).hostname or ""
    return ExternalHttpSearchProvider(
        settings.url, allowed_hosts=(host,), api_key=settings.api_key
    )


def _brave(settings: UpstreamSettings) -> SearchProvider:
    from agent_platform.search.brave import BraveSearchProvider

    if not settings.api_key:
        msg = "the brave upstream needs SEARCH_GATEWAY_API_KEY"
        raise UpstreamConfigError(msg)
    return BraveSearchProvider(settings.api_key)


UPSTREAMS: dict[str, Upstream] = {
    "fake": Upstream(_fake),
    "http-json": Upstream(_http_json, uses_url=True),
    "brave": Upstream(_brave),
}


def build_upstream(name: str, settings: UpstreamSettings) -> SearchProvider:
    upstream = UPSTREAMS.get(name)
    if upstream is None:
        known = ", ".join(sorted(UPSTREAMS))
        msg = f"unknown search upstream {name!r} (known: {known})"
        raise UpstreamConfigError(msg)
    if settings.url and not upstream.uses_url:
        msg = f"the {name} upstream takes no SEARCH_GATEWAY_UPSTREAM_URL"
        raise UpstreamConfigError(msg)
    return upstream.build(settings)


__all__ = ["UPSTREAMS", "Upstream", "UpstreamConfigError", "UpstreamSettings",
           "build_upstream"]
