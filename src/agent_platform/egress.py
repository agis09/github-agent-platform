"""Execute-job network egress level.

One deployment runs at exactly one egress level, set by the infrastructure
(terraform -> orchestrator env). A repository's level is decided by which
GitHub App (= which deployment) is installed on it; nothing in a task, an
issue or a plan can change it. See docs/design.md.

- none:      model endpoint + DNS only (the NetworkPolicy baseline)
- allowlist: + the egress proxy (smokescreen), which only CONNECTs to
             AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS
- full:      + 443 to public addresses (NetworkPolicy ipBlock, no proxy)

The enforcement is the NetworkPolicy and the proxy ACL rendered by
terraform. This module only carries the level to the execute Job (pod
label + proxy env) and to the humans (execute prompt, issue comment).
Inconsistent settings fail loudly; there is no fallback to another level.
"""

import enum
import os
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from agent_platform.errors import AgentPlatformError


class EgressConfigError(AgentPlatformError):
    """The egress settings are missing, malformed or inconsistent."""


class EgressLevel(enum.StrEnum):
    NONE = "none"
    ALLOWLIST = "allowlist"
    FULL = "full"


# Hostnames as smokescreen's ACL accepts them (lowercase punycode, an
# optional single leading "*." wildcard). The proxy ACL is the enforcement;
# this check keeps the env and the ACL from drifting into nonsense.
_HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class EgressConfig:
    level: EgressLevel
    proxy_url: str = ""
    allowed_hosts: tuple[str, ...] = ()

    @classmethod
    def from_environ(cls, env: Mapping[str, str] | None = None) -> "EgressConfig":
        source = os.environ if env is None else env
        raw_level = source.get("AGENT_PLATFORM_EGRESS_LEVEL", "none")
        try:
            level = EgressLevel(raw_level)
        except ValueError as exc:
            msg = (
                f"AGENT_PLATFORM_EGRESS_LEVEL must be one of "
                f"{[lv.value for lv in EgressLevel]}, got {raw_level!r}"
            )
            raise EgressConfigError(msg) from exc
        proxy_url = source.get("AGENT_PLATFORM_EGRESS_PROXY_URL", "")
        hosts = tuple(
            h.strip() for h in source.get("AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        )
        if level is EgressLevel.ALLOWLIST:
            _check_proxy_url(proxy_url)
            if not hosts:
                msg = "egress level 'allowlist' needs AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS"
                raise EgressConfigError(msg)
            bad = [h for h in hosts if not _HOST.match(h)]
            if bad:
                msg = f"invalid host in AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS: {bad}"
                raise EgressConfigError(msg)
        elif proxy_url or hosts:
            # A proxy URL or host list next to none/full means the infra and
            # the orchestrator disagree about the level — surface it.
            msg = (
                f"egress level {level.value!r} takes no proxy URL or allowed "
                "hosts; set AGENT_PLATFORM_EGRESS_LEVEL=allowlist or unset them"
            )
            raise EgressConfigError(msg)
        return cls(level=level, proxy_url=proxy_url, allowed_hosts=hosts)

    def describe(self) -> str:
        """One line for humans (issue comment, execute prompt)."""
        if self.level is EgressLevel.ALLOWLIST:
            return f"allowlist ({', '.join(self.allowed_hosts)})"
        if self.level is EgressLevel.FULL:
            return "full (HTTPS to public addresses)"
        return "none (no Internet access)"


def _check_proxy_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.port is None:
        msg = (
            "egress level 'allowlist' needs AGENT_PLATFORM_EGRESS_PROXY_URL "
            f"as http://<host>:<port>, got {url!r}"
        )
        raise EgressConfigError(msg)
    if parsed.path not in ("", "/") or parsed.username or parsed.query:
        msg = f"AGENT_PLATFORM_EGRESS_PROXY_URL must be a bare http://<host>:<port>, got {url!r}"
        raise EgressConfigError(msg)


__all__ = ["EgressConfig", "EgressConfigError", "EgressLevel"]
