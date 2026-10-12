"""Unit: query sanitizer — forbidden content refused before send."""

import pytest

from agent_platform.errors import DomainRejectedError
from agent_platform.search.base import SearchQuery
from agent_platform.search.sanitizer import SearchSanitizer


def _check(query: str, **kwargs):
    sanitizer = SearchSanitizer(**kwargs)
    return sanitizer.check(SearchQuery(query=query), now="2026-09-16T00:00:00Z")


def test_clean_query_accepted():
    decision = _check("python subprocess timeout best practices")
    assert decision.accepted is True
    assert decision.reason == "ok"


def test_secret_shaped_query_rejected():
    with pytest.raises(DomainRejectedError, match="secret-shaped"):
        _check("how to use token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 in requests")


def test_pem_block_rejected():
    with pytest.raises(DomainRejectedError, match="secret-shaped"):
        _check("-----BEGIN RSA PRIVATE KEY----- what is this")


def test_long_hex_rejected():
    with pytest.raises(DomainRejectedError, match="secret-shaped"):
        _check("verify checksum " + "a" * 48)


def test_internal_url_rejected():
    with pytest.raises(DomainRejectedError, match="internal URL"):
        _check("docs for gateway.internal.example service")


def test_bare_ip_rejected():
    with pytest.raises(DomainRejectedError, match="internal URL"):
        _check("connect to 10.0.0.5 vllm")


def test_stack_trace_rejected():
    with pytest.raises(DomainRejectedError, match="log fragments"):
        _check("Traceback (most recent call last) fix this error")


def test_workspace_path_rejected():
    with pytest.raises(DomainRejectedError, match="log fragments"):
        _check("error in /workspace/repo/todos_app/cli.py line 12")


def test_source_code_line_rejected():
    with pytest.raises(DomainRejectedError, match="source-code"):
        _check("import subprocess; subprocess.run(cmd)")


def test_issue_body_token_rejected():
    with pytest.raises(DomainRejectedError, match="task context"):
        _check(
            "todos.json リセット手順 の書き方を検索したい",
            issue_tokens=("todos.json リセット手順",),
        )
