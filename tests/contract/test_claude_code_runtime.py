"""Contract: LocalAgentAdapter + ClaudeCodeRuntime via a fake `claude`
binary — fully offline. The fake records its argv and environment so the
tests can check tool restrictions and key handling."""

import json
import stat
from pathlib import Path

import pytest

from agent_platform.adapters.agent.local import LocalAgentAdapter
from agent_platform.agent_runtime import ClaudeCodeRuntime, RunMode
from agent_platform.agent_runtime.claude_code import extract_json_object
from agent_platform.config import Settings
from agent_platform.errors import AdapterContractError, AdapterError, AdapterTimeoutError
from tests.contract.test_local_adapter_codex import JOB, VALID_PLAN

CANARY = "sk-ant-canary-456"


def _settings(tmp_path: Path, **overrides) -> Settings:
    data = dict(
        home_dir=str(tmp_path / "home"),
        artifact_max_size=1024 * 1024,
        default_approval_ttl_hours=72,
        agent_runtime="claude-code",
        agent_binary=str(tmp_path / "fake-claude"),
        agent_model="claude-test",
        agent_base_url=None,
        agent_api_key_env="LLM_API_KEY",
        agent_timeout_seconds=10.0,
    )
    data.update(overrides)
    return Settings(**data)


def _install_fake_claude(tmp_path: Path, *, result: dict, exit_code: int = 0,
                         sleep: float = 0) -> Path:
    record = tmp_path / "record.json"
    script = f"""#!/usr/bin/env python3
import json, os, sys, time
prompt = sys.stdin.read()
json.dump({{"argv": sys.argv[1:], "prompt": prompt,
           "api_key": os.environ.get("ANTHROPIC_API_KEY"),
           "auth_token": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
           "base_url": os.environ.get("ANTHROPIC_BASE_URL"),
           "nonessential": os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")}},
          open({str(record)!r}, "w"))
time.sleep({sleep})
print({json.dumps(result)!r})
sys.exit({exit_code})
"""
    path = tmp_path / "fake-claude"
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return record


def _result(text: str, *, is_error: bool = False) -> dict:
    return {
        "type": "result",
        "subtype": "error_during_execution" if is_error else "success",
        "is_error": is_error,
        "result": text,
        "num_turns": 3,
        "session_id": "s-1",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def test_plan_normal_read_only_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", CANARY)
    record = _install_fake_claude(tmp_path, result=_result(json.dumps(VALID_PLAN)))
    adapter = LocalAgentAdapter(_settings(tmp_path))
    plan = adapter.plan(JOB)
    assert plan.proposed_changes[0].path == "src/thing.py"
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--model") + 1] == "claude-test"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob,LS"
    disallowed = argv[argv.index("--disallowedTools") + 1].split(",")
    assert {"Bash", "Edit", "Write", "WebFetch", "WebSearch"} <= set(disallowed)
    assert "bypassPermissions" not in argv
    # schema travels in the prompt; key travels only in the child env
    assert "<output_schema>" in seen["prompt"]
    assert seen["api_key"] == CANARY
    assert seen["nonessential"] == "1"
    assert CANARY not in " ".join(argv)
    assert CANARY not in seen["prompt"]
    assert CANARY not in adapter.last_outcome.audit_line()


def test_fenced_json_is_extracted(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", CANARY)
    text = "Here is the plan:\n```json\n" + json.dumps(VALID_PLAN) + "\n```"
    _install_fake_claude(tmp_path, result=_result(text))
    plan = LocalAgentAdapter(_settings(tmp_path)).plan(JOB)
    assert plan.summary == "fix the thing"


def test_write_mode_skips_prompts_but_keeps_web_tools_off(tmp_path):
    record = _install_fake_claude(tmp_path, result=_result("done"))
    outcome = ClaudeCodeRuntime(
        _settings(tmp_path, agent_base_url="https://gw.example.invalid")
    ).run(prompt="do it", mode=RunMode.WRITE, cwd=tmp_path)
    assert outcome.ok
    assert outcome.final_message == "done"
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--disallowedTools") + 1] == "WebFetch,WebSearch"
    assert "--allowedTools" not in argv
    assert seen["base_url"] == "https://gw.example.invalid"


def test_bearer_scheme_sends_the_key_as_auth_token(tmp_path, monkeypatch):
    """Gateways such as Sakura AI Engine take Authorization: Bearer, which
    Claude Code sends for ANTHROPIC_AUTH_TOKEN (not ANTHROPIC_API_KEY)."""
    monkeypatch.setenv("LLM_API_KEY", CANARY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-shell-key")
    record = _install_fake_claude(tmp_path, result=_result("done"))
    settings = _settings(
        tmp_path, agent_auth_scheme="bearer", agent_base_url="https://api.ai.sakura.ad.jp"
    )
    assert ClaudeCodeRuntime(settings).run(prompt="x", mode=RunMode.READ_ONLY).ok
    seen = json.loads(record.read_text())
    assert seen["auth_token"] == CANARY
    assert seen["api_key"] is None  # an inherited key never reaches the CLI
    assert seen["base_url"] == "https://api.ai.sakura.ad.jp"


def test_is_error_result_fails_the_plan(tmp_path):
    _install_fake_claude(tmp_path, result=_result("rate limited", is_error=True))
    with pytest.raises(AdapterError, match="rate limited"):
        LocalAgentAdapter(_settings(tmp_path)).plan(JOB)


def test_nonzero_exit_fails_the_plan(tmp_path):
    _install_fake_claude(tmp_path, result=_result("x"), exit_code=2)
    with pytest.raises(AdapterError, match="exited with code 2"):
        LocalAgentAdapter(_settings(tmp_path)).plan(JOB)


def test_prose_answer_is_a_contract_error(tmp_path):
    _install_fake_claude(tmp_path, result=_result("I would fix it by hand."))
    with pytest.raises(AdapterContractError):
        LocalAgentAdapter(_settings(tmp_path)).plan(JOB)


def test_timeout(tmp_path):
    _install_fake_claude(tmp_path, result=_result("{}"), sleep=5)
    with pytest.raises(AdapterTimeoutError):
        LocalAgentAdapter(_settings(tmp_path, agent_timeout_seconds=0.2)).plan(JOB)


def test_missing_binary(tmp_path):
    adapter = LocalAgentAdapter(_settings(tmp_path, agent_binary="/nonexistent/claude"))
    with pytest.raises(AdapterError, match="not found"):
        adapter.plan(JOB)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('Result: {"a": {"b": 2}} done', '{"a": {"b": 2}}'),
        ("no json here", "no json here"),
    ],
)
def test_extract_json_object(text, expected):
    assert extract_json_object(text) == expected
