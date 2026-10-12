"""Coding-agent CLI runtimes, selected by AGENT_PLATFORM_AGENT_RUNTIME.

To add a runtime: implement AgentRuntime (base.py) in a new module, add
it to RUNTIMES below and to config.AGENT_RUNTIMES (default binary and API
host), and install the CLI in deploy/worker/Dockerfile.
"""

from collections.abc import Callable

from agent_platform.agent_runtime.base import AgentRuntime, RunMode, RunOutcome
from agent_platform.agent_runtime.claude_code import ClaudeCodeRuntime
from agent_platform.agent_runtime.codex import CodexRuntime
from agent_platform.config import Settings

RUNTIMES: dict[str, Callable[[Settings], AgentRuntime]] = {
    "codex": CodexRuntime,
    "claude-code": ClaudeCodeRuntime,
}


def build_runtime(settings: Settings) -> AgentRuntime:
    try:
        factory = RUNTIMES[settings.agent_runtime]
    except KeyError as exc:
        msg = f"unknown agent runtime {settings.agent_runtime!r} (known: {sorted(RUNTIMES)})"
        raise ValueError(msg) from exc
    return factory(settings)


__all__ = [
    "RUNTIMES",
    "AgentRuntime",
    "ClaudeCodeRuntime",
    "CodexRuntime",
    "RunMode",
    "RunOutcome",
    "build_runtime",
]
