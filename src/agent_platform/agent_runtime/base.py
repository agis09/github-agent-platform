"""AgentRuntime protocol: one non-interactive run of a coding-agent CLI.

A runtime wraps a CLI (codex, claude, ...) behind one call:

    runtime.run(prompt=..., mode=RunMode.READ_ONLY, cwd=..., output_schema=...)

and returns the agent's final message plus an audit summary. The plan,
execute and review workers (agent_platform.worker) and the local plan
adapter only talk to this protocol, so adding an agent CLI means adding one
runtime module and one entry in agent_runtime.RUNTIMES.

Contract every runtime keeps:
- the API key value is never placed in argv, prompts, logs or results; it
  travels from the environment to the child process only
- a timeout raises AdapterTimeoutError; a missing binary / failed start
  raises AdapterError
- a non-zero exit or an agent-reported failure is returned (not raised):
  the plan adapter treats it as an error, the execute worker records it as
  a violation
- with output_schema, final_message is the JSON text the agent produced
  for that schema (callers still validate it against the pydantic model)
"""

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class RunMode(enum.StrEnum):
    # The agent may read the working directory but not change it.
    READ_ONLY = "read-only"
    # The agent may edit files and run commands. Isolation comes from the
    # Kubernetes Pod boundary (non-root, read-only rootfs, no credentials,
    # default-deny egress), not from the CLI's own sandbox.
    WRITE = "write"


@dataclass(frozen=True)
class RunOutcome:
    runtime: str
    returncode: int
    final_message: str | None
    stdout: str
    stderr: str
    # Failure the agent reported itself (turn failure, is_error result).
    error: str | None = None
    # Event types / token usage only — never message text (see docs/design.md).
    audit: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None

    def failure_detail(self, limit: int = 400) -> str:
        if self.error:
            return self.error[-limit:]
        text = self.stderr.strip()
        return text[-limit:] if text else f"exit code {self.returncode}"

    def audit_line(self) -> str:
        fields = " ".join(f"{k}={v}" for k, v in sorted(self.audit.items()))
        return f"{self.runtime} audit rc={self.returncode} {fields}".rstrip()


class AgentRuntime(Protocol):
    name: str

    def run(
        self,
        *,
        prompt: str,
        mode: RunMode,
        cwd: Path | None = None,
        output_schema: Path | None = None,
    ) -> RunOutcome: ...
