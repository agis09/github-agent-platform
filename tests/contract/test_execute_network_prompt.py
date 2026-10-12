"""Contract: the execute prompt and the issue plan comment describe the
deployment's egress level (the enforcement is NetworkPolicy + proxy ACL)."""

import pytest

from agent_platform import cli
from agent_platform.egress import EgressConfig, EgressConfigError, EgressLevel


def test_network_section_none_forbids_installs():
    text = cli._network_section(EgressConfig(level=EgressLevel.NONE))
    assert "There is no Internet access" in text
    assert "do not attempt them" in text


def test_network_section_allowlist_names_the_hosts():
    text = cli._network_section(EgressConfig(
        level=EgressLevel.ALLOWLIST, proxy_url="http://p:4750",
        allowed_hosts=("pypi.org", "files.pythonhosted.org"),
    ))
    assert "only to these hosts, through the preconfigured proxy: " \
        "pypi.org, files.pythonhosted.org" in text
    assert "pip install --user" in text
    # The proxy URL is plumbing, not something the agent should configure.
    assert "http://p:4750" not in text


def test_network_section_full():
    text = cli._network_section(EgressConfig(level=EgressLevel.FULL))
    assert "HTTPS (port 443) to public Internet addresses is allowed" in text


def test_network_note_only_for_the_k8s_sandbox(monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_EGRESS_LEVEL", "full")
    monkeypatch.delenv("AGENT_PLATFORM_ADAPTER", raising=False)
    assert cli._network_note() == ""
    monkeypatch.setenv("AGENT_PLATFORM_ADAPTER", "k8s")
    assert cli._network_note() == "full (HTTPS to public addresses)"


def test_network_note_surfaces_a_broken_setting(monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_ADAPTER", "k8s")
    monkeypatch.setenv("AGENT_PLATFORM_EGRESS_LEVEL", "allowlist")
    monkeypatch.delenv("AGENT_PLATFORM_EGRESS_PROXY_URL", raising=False)
    with pytest.raises(EgressConfigError):
        cli._network_note()
