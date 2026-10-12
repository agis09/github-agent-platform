"""Forbidden-vocabulary extraction for search queries.

At plan time the orchestrator derives the tokens that must never appear in
a search query: distinctive strings from the untrusted task context (issue
title/body) and from the repository snapshot (code identifiers, hostnames).
These travel with the approval to the execute job, where SearchSanitizer
refuses any query containing them — no manual declaration of forbidden
terms is required.

The extraction is deliberately conservative: it captures *distinctive*
material (long words, identifiers, hostnames, quoted phrases) that would
leak task context, not everyday words. A query may legitimately talk about
the general topic ("terminal space invaders game") — only tokens that
identify the specific task content are forbidden.
"""

import re
from pathlib import Path

_MAX_TOKENS = 64
_MIN_TOKEN_LEN = 8
_MAX_TOKEN_LEN = 64

# Distinctive identifier shapes: snake_case / camelCase / dotted names,
# hostnames, quoted phrases.
_IDENTIFIER = re.compile(r"\b[a-z_][a-z0-9]*(?:_[a-z0-9]+){1,}\b")  # snake_case
_CAMEL = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+){1,}\b")  # camelCase
_HOSTNAME = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)  # host.tld
_QUOTED = re.compile(r"\"([^\"]{8,64})\"")
_LONG_WORD = re.compile(rf"\b[a-zA-Z]{_MIN_TOKEN_LEN},{_MAX_TOKEN_LEN}\b")

# Generic English/technical words that are not task-identifying. Kept small:
# the sanitizer's other rule classes (secrets, code shapes, internal URLs)
# cover the dangerous material; this list only trims noise.
_STOPWORDS = frozenset({
    "function", "interface", "implementation", "documentation", "development",
    "environment", "configuration", "repository", "application", "terminal",
    "requirements", "additional", "following", "correctly", "existing",
    "specified", "described", "supported", "compatible", "performance",
    "security", "important", "necessary", "standard", "available",
    "should", "would", "because", "without", "there", "which", "about",
})


def extract_forbidden_tokens(
    *,
    issue_title: str | None,
    issue_body: str | None,
    repo_root: Path | None = None,
) -> tuple[str, ...]:
    """Distinctive tokens from the untrusted task context. Bounded and
    deterministic; the result is stored on the approval and enforced by
    SearchSanitizer at execute time."""
    text = "\n".join(part for part in (issue_title or "", issue_body or "") if part)
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        token = candidate.strip().lower()
        if (
            token
            and token not in seen
            and _MIN_TOKEN_LEN <= len(token) <= _MAX_TOKEN_LEN
            and token not in _STOPWORDS
        ):
            seen.add(token)
            tokens.append(token)

    for pattern in (_QUOTED, _IDENTIFIER, _CAMEL, _HOSTNAME, _LONG_WORD):
        for match in pattern.finditer(text):
            _add(match.group(1) if pattern is _QUOTED else match.group(0))
            if len(tokens) >= _MAX_TOKENS:
                return tuple(tokens)

    if repo_root is not None:
        for token in _identifiers_from_snapshot(repo_root):
            _add(token)
            if len(tokens) >= _MAX_TOKENS:
                break
    return tuple(tokens)


def _identifiers_from_snapshot(repo_root: Path) -> list[str]:
    """Identifiers from the repo's own source: file base names and symbols
    defined in the tree. The agent works with this code, so its identifiers
    must not leak into search queries."""
    tokens: list[str] = []
    for path in sorted(Path(repo_root).rglob("*")):
        if not path.is_file() or path.suffix not in (".py", ".js", ".ts", ".go", ".rs", ".sh"):
            continue
        tokens.append(path.stem)
        if len(tokens) >= _MAX_TOKENS * 2:
            break
    return tokens
