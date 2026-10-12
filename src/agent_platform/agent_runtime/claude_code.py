"""ClaudeCodeRuntime: `claude -p --output-format json` (Claude Code CLI).

- Endpoint: the Anthropic API by default; AGENT_PLATFORM_AGENT_BASE_URL is
  passed as ANTHROPIC_BASE_URL for an Anthropic-compatible gateway/proxy.
- Key: the value of the env var named by Settings.agent_api_key_env is
  handed to the child as ANTHROPIC_API_KEY (x-api-key header), or as
  ANTHROPIC_AUTH_TOKEN (Authorization: Bearer) when agent_auth_scheme is
  "bearer". It never enters argv or logs.
- READ_ONLY allows only the read tools; WRITE skips permission prompts
  (the Pod is the isolation boundary, as with codex). Built-in web tools
  are always disabled: web access goes through the platform's search CLI
  and the deployment's egress level only.
- Structured output: the JSON Schema is embedded in the prompt and the
  JSON object is extracted from the final message. The caller validates
  it against the pydantic model, so a malformed answer is a contract
  error, never a silent pass.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from agent_platform.agent_runtime.base import RunMode, RunOutcome
from agent_platform.config import Settings
from agent_platform.errors import AdapterError, AdapterTimeoutError

_READ_TOOLS = "Read,Grep,Glob,LS"
_WEB_TOOLS = "WebFetch,WebSearch"
_WRITE_TOOLS = "Bash,Edit,MultiEdit,Write,NotebookEdit"

_SCHEMA_INSTRUCTION = """

Output format: your final answer must be exactly ONE JSON object that
conforms to the JSON Schema below. Do not wrap it in prose or markdown.

<output_schema>
{schema}
</output_schema>
"""


class ClaudeCodeRuntime:
    name = "claude-code"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def run(
        self,
        *,
        prompt: str,
        mode: RunMode,
        cwd: Path | None = None,
        output_schema: Path | None = None,
    ) -> RunOutcome:
        settings = self._settings
        binary = shutil.which(settings.agent_binary)
        if binary is None:
            msg = f"claude binary not found: {settings.agent_binary!r}"
            raise AdapterError(msg)

        cmd = [binary, "-p", "--output-format", "json"]
        if settings.agent_model:
            cmd += ["--model", settings.agent_model]
        if mode is RunMode.READ_ONLY:
            cmd += [
                "--allowedTools", _READ_TOOLS,
                "--disallowedTools", f"{_WRITE_TOOLS},{_WEB_TOOLS}",
            ]
        else:
            cmd += [
                "--permission-mode", "bypassPermissions",
                "--disallowedTools", _WEB_TOOLS,
            ]
        if output_schema is not None:
            prompt += _SCHEMA_INSTRUCTION.format(schema=output_schema.read_text().strip())

        try:
            proc = subprocess.run(  # noqa: S603 — resolved binary, no shell
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=settings.agent_timeout_seconds,
                env=_child_env(settings),
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"claude exceeded timeout of {settings.agent_timeout_seconds}s"
            raise AdapterTimeoutError(msg) from exc
        except OSError as exc:
            msg = f"failed to start claude: {exc}"
            raise AdapterError(msg) from exc

        result = _parse_result(proc.stdout)
        if result is None:
            return RunOutcome(
                runtime=self.name, returncode=proc.returncode, final_message=None,
                stdout=proc.stdout, stderr=proc.stderr,
                error=None if proc.returncode else "claude produced no JSON result",
            )
        text = result.get("result")
        final = text if isinstance(text, str) else None
        if final is not None and output_schema is not None:
            final = extract_json_object(final)
        error = None
        if result.get("is_error"):
            error = f"{result.get('subtype', 'error')}: {final or ''}".strip()
        raw_usage = result.get("usage")
        usage: dict[str, object] = raw_usage if isinstance(raw_usage, dict) else {}
        audit: dict[str, object] = {
            "session": result.get("session_id"),
            "turns": result.get("num_turns"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }
        return RunOutcome(
            runtime=self.name,
            returncode=proc.returncode,
            final_message=final,
            stdout=proc.stdout,
            stderr=proc.stderr,
            error=error,
            audit=audit,
        )


def _child_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    # Never inherit a key the operator's shell happens to export: only the
    # configured variable reaches the CLI, in the configured header.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    key = os.environ.get(settings.agent_api_key_env)
    if key:
        # api-key -> x-api-key header (Anthropic API); bearer ->
        # Authorization: Bearer (gateways such as Sakura AI Engine).
        bearer = settings.agent_auth_scheme == "bearer"
        env["ANTHROPIC_AUTH_TOKEN" if bearer else "ANTHROPIC_API_KEY"] = key
    if settings.agent_base_url:
        env["ANTHROPIC_BASE_URL"] = settings.agent_base_url
    # The sandbox egress allows the model endpoint only: no telemetry,
    # error reporting or auto-update traffic.
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


def _parse_result(stdout: str) -> dict[str, object] | None:
    """The json output format prints one result object; tolerate leading
    noise lines by taking the last line that parses as an object."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def extract_json_object(text: str) -> str:
    """Return the JSON object inside `text` (bare, or in a ``` fence).
    Falls back to the text unchanged; the caller's validation rejects it."""
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = body.rsplit("```", 1)[0].strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        return text
    candidate = stripped[start : end + 1]
    try:
        json.loads(candidate)
    except json.JSONDecodeError:
        return text
    return candidate
