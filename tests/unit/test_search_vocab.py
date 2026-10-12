"""Forbidden-vocabulary extraction (search/vocab.py)."""

from pathlib import Path

from agent_platform.search.vocab import extract_forbidden_tokens


def test_extracts_identifiers_from_issue_body():
    tokens = extract_forbidden_tokens(
        issue_title="Fix invaders.py rendering",
        issue_body="The `Game.draw` method and `Enemy.swarm()` ignore _wrap_step logic.",
        repo_root=None,
    )
    assert "invaders.py" in tokens
    assert "game.draw" in tokens
    assert "enemy.swarm" in tokens
    assert "_wrap_step" in tokens


def test_extracts_quoted_phrases_and_hostnames():
    tokens = extract_forbidden_tokens(
        issue_title=None,
        issue_body='See the "migration plan" on wiki.internal.example for details.',
        repo_root=None,
    )
    assert "migration plan" in tokens
    assert any("wiki.internal.example" in t for t in tokens)


def test_bounded_and_lowercased():
    body = " ".join(f"identifier_number_{i}" for i in range(200))
    tokens = extract_forbidden_tokens(issue_title=None, issue_body=body, repo_root=None)
    assert len(tokens) <= 64
    assert all(t == t.lower() for t in tokens)


def test_empty_context_yields_nothing():
    assert extract_forbidden_tokens(issue_title=None, issue_body=None, repo_root=None) == ()


def test_snapshot_identifiers_included(tmp_path: Path):
    (tmp_path / "game_engine.py").write_text("class Engine: ...\n")
    tokens = extract_forbidden_tokens(
        issue_title="Fix the engine", issue_body=None, repo_root=tmp_path
    )
    assert "game_engine" in tokens


def test_stopwords_excluded():
    tokens = extract_forbidden_tokens(
        issue_title=None,
        issue_body="This is a documentation issue about the interface.",
        repo_root=None,
    )
    assert "documentation" not in tokens
    assert "interface" not in tokens
