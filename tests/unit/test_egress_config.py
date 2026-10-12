"""Unit: execute-job egress level settings (EgressConfig)."""

import pytest

from agent_platform.egress import EgressConfig, EgressConfigError, EgressLevel

PROXY = "http://egress-proxy.agent-platform.svc.cluster.local:4750"


def test_unset_level_is_none():
    config = EgressConfig.from_environ({})
    assert config.level is EgressLevel.NONE
    assert config.proxy_url == ""
    assert config.allowed_hosts == ()
    assert config.describe() == "none (no Internet access)"


def test_allowlist_reads_proxy_and_hosts():
    config = EgressConfig.from_environ({
        "AGENT_PLATFORM_EGRESS_LEVEL": "allowlist",
        "AGENT_PLATFORM_EGRESS_PROXY_URL": PROXY,
        "AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS": "pypi.org, files.pythonhosted.org,*.example.co.jp",
    })
    assert config.level is EgressLevel.ALLOWLIST
    assert config.proxy_url == PROXY
    assert config.allowed_hosts == ("pypi.org", "files.pythonhosted.org", "*.example.co.jp")
    assert config.describe() == (
        "allowlist (pypi.org, files.pythonhosted.org, *.example.co.jp)"
    )


def test_full_level():
    config = EgressConfig.from_environ({"AGENT_PLATFORM_EGRESS_LEVEL": "full"})
    assert config.level is EgressLevel.FULL
    assert config.describe() == "full (HTTPS to public addresses)"


def test_unknown_level_is_rejected():
    with pytest.raises(EgressConfigError, match="must be one of"):
        EgressConfig.from_environ({"AGENT_PLATFORM_EGRESS_LEVEL": "trusted"})


@pytest.mark.parametrize("url", [
    "", "https://proxy:4750", "http://proxy", "http://proxy:4750/path",
    "http://user@proxy:4750", "proxy:4750",
])
def test_allowlist_needs_a_bare_http_proxy_url(url):
    with pytest.raises(EgressConfigError, match="PROXY_URL"):
        EgressConfig.from_environ({
            "AGENT_PLATFORM_EGRESS_LEVEL": "allowlist",
            "AGENT_PLATFORM_EGRESS_PROXY_URL": url,
            "AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS": "pypi.org",
        })


def test_allowlist_needs_hosts():
    with pytest.raises(EgressConfigError, match="ALLOWED_HOSTS"):
        EgressConfig.from_environ({
            "AGENT_PLATFORM_EGRESS_LEVEL": "allowlist",
            "AGENT_PLATFORM_EGRESS_PROXY_URL": PROXY,
            "AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS": " , ",
        })


@pytest.mark.parametrize("host", [
    "PyPI.org", "*.*.example.com", "example.*", "pypi.org:443", "10.0.0.1", "localhost",
])
def test_allowlist_rejects_malformed_hosts(host):
    with pytest.raises(EgressConfigError, match="invalid host"):
        EgressConfig.from_environ({
            "AGENT_PLATFORM_EGRESS_LEVEL": "allowlist",
            "AGENT_PLATFORM_EGRESS_PROXY_URL": PROXY,
            "AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS": host,
        })


@pytest.mark.parametrize("level", ["none", "full"])
@pytest.mark.parametrize("extra", [
    {"AGENT_PLATFORM_EGRESS_PROXY_URL": PROXY},
    {"AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS": "pypi.org"},
])
def test_proxy_settings_next_to_a_non_allowlist_level_are_rejected(level, extra):
    """The infra and the orchestrator disagreeing about the level is an
    error, not a silent pick of one side."""
    with pytest.raises(EgressConfigError, match="takes no proxy URL"):
        EgressConfig.from_environ({"AGENT_PLATFORM_EGRESS_LEVEL": level, **extra})
