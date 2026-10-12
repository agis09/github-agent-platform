"""CodexRuntime: `codex exec --json` against any OpenAI Responses-compatible
endpoint (OpenAI, vLLM, LiteLLM, Azure OpenAI, ...).

Secrets: the runtime never handles the key value. The generated provider
config names the env var holding it (Settings.agent_api_key_env); the
value travels from the environment to the codex process directly.

The provider config must live at user level ($CODEX_HOME/config.toml) —
a project-local `.codex/config.toml` ignores model_providers — so a
disposable CODEX_HOME is generated per run.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from agent_platform import codex_events
from agent_platform.agent_runtime.base import RunMode, RunOutcome
from agent_platform.codex_schema import write_gateway_schema
from agent_platform.config import Settings
from agent_platform.errors import AdapterError, AdapterTimeoutError

_PROVIDER_ID = "agent_platform"


class CodexRuntime:
    name = "codex"

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
            msg = f"codex binary not found: {settings.agent_binary!r}"
            raise AdapterError(msg)

        # ignore_cleanup_errors: codex may leave plugin-clone temp dirs inside
        # CODEX_HOME; a best-effort leftover must not fail a completed run.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
            codex_home = Path(home)
            write_provider_config(codex_home, settings)
            cmd = [binary, "exec", "--json", "--skip-git-repo-check"]
            # READ_ONLY uses codex's own sandbox. WRITE relies on the Pod
            # boundary: codex's workspace-write sandbox needs bubblewrap user
            # namespaces, which Pod Security "restricted" blocks.
            sandbox = "read-only" if mode is RunMode.READ_ONLY else "danger-full-access"
            cmd += ["--sandbox", sandbox]
            if output_schema is not None:
                # codex gets the gateway-compatible form; the result is still
                # validated against the pydantic model (codex_schema.py).
                cmd += ["--output-schema", str(write_gateway_schema(output_schema, codex_home))]
            if settings.agent_model:
                cmd += ["-c", f'model="{settings.agent_model}"']
            if settings.agent_max_output_tokens is not None:
                cmd += ["-c", f"model_max_output_tokens={settings.agent_max_output_tokens}"]
            cmd.append("-")  # prompt from stdin
            try:
                proc = subprocess.run(  # noqa: S603 — resolved binary, no shell
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=settings.agent_timeout_seconds,
                    env={**os.environ, "CODEX_HOME": str(codex_home)},
                )
            except subprocess.TimeoutExpired as exc:
                msg = f"codex exceeded timeout of {settings.agent_timeout_seconds}s"
                raise AdapterTimeoutError(msg) from exc
            except OSError as exc:
                msg = f"failed to start codex: {exc}"
                raise AdapterError(msg) from exc

        lines = proc.stdout.splitlines()
        try:
            summary = codex_events.summarize_stream(lines)
        except codex_events.CodexEventError as exc:
            if proc.returncode == 0:
                raise
            # A crashed codex may leave a truncated stream; the exit code is
            # the failure, the parse error is only detail.
            return RunOutcome(
                runtime=self.name, returncode=proc.returncode, final_message=None,
                stdout=proc.stdout, stderr=proc.stderr, error=None,
                audit={"stream": f"unparsable ({exc})"},
            )
        usage = summary.usage
        audit: dict[str, object] = {
            "thread": summary.thread_id,
            "messages": len(summary.agent_messages),
        }
        if usage is not None:
            audit.update(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_output_tokens,
            )
        return RunOutcome(
            runtime=self.name,
            returncode=proc.returncode,
            final_message=summary.last_agent_message,
            stdout=proc.stdout,
            stderr=proc.stderr,
            error=summary.fatal_error,
            audit=audit,
        )


def write_provider_config(codex_home: Path, settings: Settings) -> None:
    """Write $CODEX_HOME/config.toml with one custom provider. Without a
    base URL the provider points at OpenAI; the key env var name is always
    the configured one, so one secret name works for every endpoint."""
    codex_home.mkdir(parents=True, mode=0o700, exist_ok=True)
    base_url = settings.agent_base_url or "https://api.openai.com/v1"
    lines = []
    if settings.agent_model:
        lines.append(f'model = "{settings.agent_model}"')
    lines += [
        f'model_provider = "{_PROVIDER_ID}"',
        "",
        f"[model_providers.{_PROVIDER_ID}]",
        'name = "agent-platform"',
        f'base_url = "{base_url}"',
        f'env_key = "{settings.agent_api_key_env}"',
        f'wire_api = "{settings.codex_wire_api}"',
    ]
    config_path = codex_home / "config.toml"
    config_path.write_text("\n".join(lines) + "\n")
    config_path.chmod(0o600)
