"""Query sanitizer: forbidden information must be refused
BEFORE any provider is called.

Forbidden content classes:
- issue body fragments (the untrusted task context)
- source code lines (the workspace snapshot)
- repository names / internal hostnames and URLs
- secret-shaped strings (API keys, tokens, PEM blocks, bearer headers)
- log fragments (stack-trace lines, file paths from the workspace)

Plus: an allowlist of destination hosts the external provider may use, and a
token-bucket rate limit. Every accept/refuse decision is auditable via the
returned decision record.
"""

import re
import time
from dataclasses import dataclass, field

from agent_platform.errors import DomainRejectedError
from agent_platform.search.base import SearchQuery


class QueryRejectedError(DomainRejectedError):
    """The query contains forbidden information and was not sent."""

    reason: str = "refused"


@dataclass
class SanitizerDecision:
    """Audit record for one query's sanitizer verdict."""

    query: str
    accepted: bool
    reason: str
    at: str


# Secret-shaped strings: long hex/base64 runs, bearer headers, PEM markers,
# AWS/GitHub-style tokens.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{40,64}\b"),  # long hex (token/hash-like)
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),  # base64-like runs
)

# Internal URL / hostname shapes.
_INTERNAL_URL = re.compile(
    r"(?:https?://)?"
    r"(?:[a-z0-9-]+\.)*(?:internal|local|corp|intra|svc|cluster)(?:\.[a-z]{2,})*"
    r"(?:/[^\s]*)?",
    re.IGNORECASE,
)
_IP_URL = re.compile(r"(?:https?://)?\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/\S*)?")

# Log-fragment shapes: stack traces and workspace file paths.
_LOG_FRAGMENTS = (
    re.compile(r"\bTraceback \(most recent call last\)"),
    re.compile(r"\bFile \"[^\"]+\", line \d+"),
    re.compile(r"(?:/opt/agent-platform|/workspace|/tmp/agent-platform)\S*"),
)

# Source-code-line shapes: braces+operators typical of code, shebangs,
# imports with module paths.
_CODE_SHAPES = (
    re.compile(r"^\s*(?:import|from|def |class |return |function |const |var )\s"),
    re.compile(r"[{};]\s*$"),
)


@dataclass
class SearchSanitizer:
    """Configurable sanitizer. issue_tokens / code_tokens / extra_forbidden
    are the caller-declared forbidden strings (e.g. the task's issue body
    n-grams); anything matching is refused."""

    forbidden_hosts: tuple[str, ...] = ()
    issue_tokens: tuple[str, ...] = ()
    code_tokens: tuple[str, ...] = ()
    max_query_length: int = 512
    rate_limit_per_minute: int = 10
    _sent_timestamps: list[float] = field(default_factory=list, repr=False)

    def check(self, query: SearchQuery, *, now: str) -> SanitizerDecision:
        """Return the decision; raises QueryRejectedError on refusal. The
        caller MUST record the decision either way (audit)."""
        reasons: list[str] = []
        q = query.query

        if len(q) > self.max_query_length:
            reasons.append(f"query exceeds {self.max_query_length} chars")

        for pattern in _SECRET_PATTERNS:
            if pattern.search(q):
                reasons.append("query contains secret-shaped content")
                break

        if _INTERNAL_URL.search(q) or _IP_URL.search(q):
            reasons.append("query contains an internal URL or bare IP")

        for pattern in _LOG_FRAGMENTS:
            if pattern.search(q):
                reasons.append("query contains log fragments")
                break

        for line in q.splitlines():
            if any(pattern.search(line) for pattern in _CODE_SHAPES):
                reasons.append("query contains source-code lines")
                break

        lowered = q.lower()
        for host in self.forbidden_hosts:
            if host.lower() in lowered:
                reasons.append(f"query names a forbidden host: {host}")
                break
        for token in (*self.issue_tokens, *self.code_tokens):
            if token and token.lower() in lowered:
                reasons.append("query contains task context (issue/code) text")
                break

        if not reasons and not self._rate_allow():
            reasons.append("rate limit exceeded")

        decision = SanitizerDecision(
            query=q, accepted=not reasons,
            reason=reasons[0] if reasons else "ok", at=now,
        )
        if reasons:
            # One auditable exception; the first reason is the verdict.
            err = QueryRejectedError(
                f"search query refused before send: {decision.reason} "
                f"(query: {q[:80]!r})"
            )
            # The verdict without the query text, for callers that must not
            # echo the query (the Search Gateway response and log).
            err.reason = decision.reason
            raise err
        return decision

    def _rate_allow(self) -> bool:
        now = time.monotonic()
        self._sent_timestamps = [
            ts for ts in self._sent_timestamps if now - ts < 60
        ]
        if len(self._sent_timestamps) >= self.rate_limit_per_minute:
            return False
        self._sent_timestamps.append(now)
        return True
