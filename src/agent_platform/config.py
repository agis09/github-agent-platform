"""Application settings sourced from environment variables.

Secrets never enter Settings — only non-secret endpoint URLs, model names,
and the *names* of env vars that hold secrets (resolved later by the agent
runtime). Unknown values fail loudly; there is no silent fallback.

Agent selection (see docs/configuration.md):

- AGENT_PLATFORM_AGENT_RUNTIME       codex (default) | claude-code
- AGENT_PLATFORM_AGENT_BINARY        CLI path/name (default per runtime)
- AGENT_PLATFORM_AGENT_MODEL         model name passed to the CLI
- AGENT_PLATFORM_AGENT_BASE_URL      model endpoint (OpenAI-compatible for
                                     codex, Anthropic-compatible for
                                     claude-code); unset = vendor default
- AGENT_PLATFORM_AGENT_API_KEY_ENV   NAME of the env var holding the API key
                                     (default LLM_API_KEY)
- AGENT_PLATFORM_AGENT_TIMEOUT_SECONDS
- AGENT_PLATFORM_AGENT_MAX_OUTPUT_TOKENS  (codex only)
- AGENT_PLATFORM_AGENT_AUTH_SCHEME   claude-code only: api-key (x-api-key
                                     header, default) | bearer
                                     (Authorization: Bearer, for gateways
                                     such as Sakura AI Engine)
- AGENT_PLATFORM_CODEX_WIRE_API      codex only; "responses"
"""

import os
import urllib.parse
from dataclasses import dataclass

# runtime name -> (default binary, default API host)
AGENT_RUNTIMES: dict[str, tuple[str, str]] = {
    "codex": ("codex", "api.openai.com"),
    "claude-code": ("claude", "api.anthropic.com"),
}

DEFAULT_API_KEY_ENV = "LLM_API_KEY"

AUTH_SCHEMES = ("api-key", "bearer")


@dataclass(frozen=True)
class Settings:
    home_dir: str
    artifact_max_size: int
    default_approval_ttl_hours: int

    # Agent runtime (secret values are never stored here — only the *name*
    # of the environment variable that holds the API key).
    agent_runtime: str = "codex"
    agent_binary: str = "codex"
    agent_model: str | None = None
    agent_base_url: str | None = None
    agent_api_key_env: str = DEFAULT_API_KEY_ENV
    agent_timeout_seconds: float = 600.0
    agent_max_output_tokens: int | None = None
    agent_auth_scheme: str = "api-key"
    codex_wire_api: str = "responses"

    @classmethod
    def from_environ(cls, env: dict[str, str] | None = None) -> "Settings":
        source = dict(os.environ if env is None else env)
        runtime = source.get("AGENT_PLATFORM_AGENT_RUNTIME") or "codex"
        if runtime not in AGENT_RUNTIMES:
            msg = (
                f"AGENT_PLATFORM_AGENT_RUNTIME must be one of "
                f"{sorted(AGENT_RUNTIMES)}, got {runtime!r}"
            )
            raise ValueError(msg)
        codex_wire_api = source.get("AGENT_PLATFORM_CODEX_WIRE_API", "responses")
        if codex_wire_api != "responses":
            msg = "AGENT_PLATFORM_CODEX_WIRE_API must be 'responses'"
            raise ValueError(msg)
        auth_scheme = source.get("AGENT_PLATFORM_AGENT_AUTH_SCHEME") or "api-key"
        if auth_scheme not in AUTH_SCHEMES:
            msg = (
                f"AGENT_PLATFORM_AGENT_AUTH_SCHEME must be one of {list(AUTH_SCHEMES)}, "
                f"got {auth_scheme!r}"
            )
            raise ValueError(msg)
        base_url = source.get("AGENT_PLATFORM_AGENT_BASE_URL") or None
        if base_url is not None:
            _check_base_url(base_url)
        return cls(
            home_dir=source.get("AGENT_PLATFORM_HOME", ".agent-platform"),
            artifact_max_size=_env_int_from(
                source, "AGENT_PLATFORM_ARTIFACT_MAX_SIZE", 16 * 1024 * 1024
            ),
            default_approval_ttl_hours=_env_int_from(
                source, "AGENT_PLATFORM_APPROVAL_TTL_HOURS", 72
            ),
            agent_runtime=runtime,
            agent_binary=(
                source.get("AGENT_PLATFORM_AGENT_BINARY") or AGENT_RUNTIMES[runtime][0]
            ),
            agent_model=source.get("AGENT_PLATFORM_AGENT_MODEL") or None,
            agent_base_url=base_url,
            agent_api_key_env=(
                source.get("AGENT_PLATFORM_AGENT_API_KEY_ENV") or DEFAULT_API_KEY_ENV
            ),
            agent_timeout_seconds=_env_float_from(
                source, "AGENT_PLATFORM_AGENT_TIMEOUT_SECONDS", 600.0
            ),
            agent_max_output_tokens=_env_int_opt_from(
                source, "AGENT_PLATFORM_AGENT_MAX_OUTPUT_TOKENS"
            ),
            agent_auth_scheme=auth_scheme,
            codex_wire_api=codex_wire_api,
        )

    @property
    def llm_host(self) -> str:
        """Host the agent talks to: the configured endpoint, else the
        runtime vendor's default API host."""
        if self.agent_base_url:
            host = urllib.parse.urlsplit(self.agent_base_url).hostname
            if host:
                return host
        return AGENT_RUNTIMES[self.agent_runtime][1]


def _check_base_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        msg = f"AGENT_PLATFORM_AGENT_BASE_URL must be an http(s) URL, got {url!r}"
        raise ValueError(msg)


def _env_int_from(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"environment variable {name} must be an integer, got {raw!r}"
        raise ValueError(msg) from exc


def _env_float_from(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        msg = f"environment variable {name} must be a number, got {raw!r}"
        raise ValueError(msg) from exc


def _env_int_opt_from(env: dict[str, str], name: str) -> int | None:
    raw = env.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"environment variable {name} must be an integer, got {raw!r}"
        raise ValueError(msg) from exc


__all__ = ["AGENT_RUNTIMES", "AUTH_SCHEMES", "DEFAULT_API_KEY_ENV", "Settings"]
