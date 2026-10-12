"""Settings parsing: env values, defaults, no secrets admitted."""

import pytest

from agent_platform.config import Settings


def test_defaults():
    settings = Settings.from_environ({})
    assert settings.home_dir == ".agent-platform"
    assert settings.default_approval_ttl_hours == 72
    assert settings.agent_runtime == "codex"
    assert settings.agent_binary == "codex"
    assert settings.agent_api_key_env == "LLM_API_KEY"
    assert settings.codex_wire_api == "responses"
    assert settings.agent_model is None
    assert settings.agent_base_url is None
    assert settings.llm_host == "api.openai.com"


def test_overrides():
    env = {
        "AGENT_PLATFORM_HOME": "state-dir-x",
        "AGENT_PLATFORM_APPROVAL_TTL_HOURS": "24",
        "AGENT_PLATFORM_AGENT_MODEL": "qwen3",
        "AGENT_PLATFORM_AGENT_BASE_URL": "http://localhost:8000/v1",
        "AGENT_PLATFORM_AGENT_TIMEOUT_SECONDS": "30.5",
        "AGENT_PLATFORM_AGENT_MAX_OUTPUT_TOKENS": "8",
        "AGENT_PLATFORM_AGENT_API_KEY_ENV": "MY_KEY",
    }
    settings = Settings.from_environ(env)
    assert settings.home_dir == "state-dir-x"
    assert settings.default_approval_ttl_hours == 24
    assert settings.agent_model == "qwen3"
    assert settings.agent_base_url == "http://localhost:8000/v1"
    assert settings.agent_timeout_seconds == 30.5
    assert settings.agent_max_output_tokens == 8
    assert settings.agent_api_key_env == "MY_KEY"
    assert settings.llm_host == "localhost"


def test_claude_code_runtime_defaults():
    settings = Settings.from_environ({"AGENT_PLATFORM_AGENT_RUNTIME": "claude-code"})
    assert settings.agent_binary == "claude"
    assert settings.llm_host == "api.anthropic.com"


def test_empty_values_mean_unset():
    # Kubernetes renders unset optional settings as "".
    settings = Settings.from_environ(
        {"AGENT_PLATFORM_AGENT_MODEL": "", "AGENT_PLATFORM_AGENT_BASE_URL": ""}
    )
    assert settings.agent_model is None
    assert settings.agent_base_url is None


def test_auth_scheme():
    assert Settings.from_environ({}).agent_auth_scheme == "api-key"
    env = {"AGENT_PLATFORM_AGENT_AUTH_SCHEME": "bearer"}
    assert Settings.from_environ(env).agent_auth_scheme == "bearer"
    with pytest.raises(ValueError, match="AGENT_PLATFORM_AGENT_AUTH_SCHEME"):
        Settings.from_environ({"AGENT_PLATFORM_AGENT_AUTH_SCHEME": "basic"})


def test_unknown_runtime_fails_loudly():
    with pytest.raises(ValueError, match="AGENT_PLATFORM_AGENT_RUNTIME"):
        Settings.from_environ({"AGENT_PLATFORM_AGENT_RUNTIME": "nope"})


def test_bad_base_url_fails_loudly():
    with pytest.raises(ValueError, match="AGENT_PLATFORM_AGENT_BASE_URL"):
        Settings.from_environ({"AGENT_PLATFORM_AGENT_BASE_URL": "localhost:8000"})


def test_invalid_int_fails_loudly():
    with pytest.raises(ValueError):
        Settings.from_environ({"AGENT_PLATFORM_APPROVAL_TTL_HOURS": "abc"})


def test_wire_api_only_responses():
    with pytest.raises(ValueError):
        Settings.from_environ({"AGENT_PLATFORM_CODEX_WIRE_API": "chat"})


def test_settings_holds_no_secret_values():
    env = {"LLM_API_KEY": "super-secret-value", "GITHUB_TOKEN": "ghp_x"}
    settings = Settings.from_environ(env)
    dumped = repr(settings) + str(settings.__dict__)
    assert "super-secret-value" not in dumped
    assert "ghp_x" not in dumped
