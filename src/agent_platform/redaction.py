"""Log redaction: defense in depth against secret leakage.

Primary defense is structural — logs only ever contain task ids, plan hashes,
statuses, paths, digests. Redaction is the second layer: planted secrets and
credential-shaped values are replaced with [REDACTED] before output.
Over-redaction is acceptable; under-redaction is not.
"""

import logging
import os
import re
import sys
from collections.abc import Mapping
from typing import Final, TextIO

_REDACTED: Final = "[REDACTED]"

# Environment variable *names* that hold secrets.
_SECRET_ENV_NAME: Final = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)"
)

# Credential-shaped values. Unknown future shapes are covered by registering
# known secret *values* via SecretRegistry (env-scan + explicit registration).
_TOKEN_PATTERNS: Final = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[=:]\s*\S+"),
)

_MIN_REGISTERED_LENGTH: Final = 8


class SecretRegistry:
    """Holds known secret *values*; replaces them anywhere in text."""

    def __init__(self) -> None:
        self._values: list[str] = []

    @classmethod
    def from_environ(cls, env: Mapping[str, str]) -> "SecretRegistry":
        registry = cls()
        for name, value in env.items():
            if _SECRET_ENV_NAME.search(name) and len(value) >= _MIN_REGISTERED_LENGTH:
                registry.register(value)
        return registry

    def register(self, value: str) -> None:
        if len(value) >= _MIN_REGISTERED_LENGTH:
            self._values.append(value)
            self._values.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        result = text
        for value in self._values:
            if value in result:
                result = result.replace(value, _REDACTED)
        for pattern in _TOKEN_PATTERNS:
            result = pattern.sub(_REDACTED, result)
        return result


class RedactingFormatter(logging.Formatter):
    """Formatter that applies SecretRegistry to the fully-expanded record."""

    def __init__(
        self,
        registry: SecretRegistry,
        fmt: str | None = None,
        datefmt: str | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._registry = registry

    def format(self, record: logging.LogRecord) -> str:
        return self._registry.redact(super().format(record))


def configure_logging(
    level: str = "INFO",
    *,
    env: Mapping[str, str] = os.environ,
    stream: TextIO = sys.stderr,
) -> SecretRegistry:
    """Attach a RedactingFormatter to the root logger; return the registry."""
    registry = SecretRegistry.from_environ(env)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(registry))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    return registry


def redact_text(text: str, *, env: Mapping[str, str] = os.environ) -> str:
    """One-shot redaction for CLI error output (same rules as the formatter)."""
    return SecretRegistry.from_environ(env).redact(text)
