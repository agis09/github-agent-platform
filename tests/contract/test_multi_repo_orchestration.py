"""Contract: candidate proposal, parent aggregation, non-interference
(one child's failure/re-execution must not duplicate another's work)."""

import datetime

from agent_platform.orchestrator.candidates import propose_candidates

UTC = datetime.UTC


def test_candidates_rank_by_keyword_overlap():
    repos = [
        "example-org/todos-app",
        "example-org/github-agent-platform",
        "example-org/unrelated-blog",
    ]
    issue = "todos_app の重複タイトル検出を修正したい。todos.json の扱いも見直す。"
    candidates = propose_candidates(issue_text=issue, repositories=repos)
    assert candidates[0].repository == "example-org/todos-app"
    assert "todos" in candidates[0].matched_keywords
    # Unrelated repos score 0 and are excluded.
    assert all(c.repository != "example-org/unrelated-broken" for c in candidates)


def test_candidates_empty_when_no_match():
    candidates = propose_candidates(
        issue_text="全く無関係な依頼内容", repositories=["example-org/todos-app"]
    )
    assert candidates == []


def test_candidates_limit():
    repos = [f"example-org/todos-{i}" for i in range(10)]
    candidates = propose_candidates(
        issue_text="todos の修正", repositories=repos, max_candidates=3
    )
    assert len(candidates) <= 3


def test_candidates_stopwords_removed():
    candidates = propose_candidates(
        issue_text="please update the todos documentation",
        repositories=["example-org/todos-app", "example-org/update-tool"],
    )
    # "please"/"the" are stop words; "update" and "todos" match repos.
    assert candidates[0].repository == "example-org/todos-app"
    matched = candidates[0].matched_keywords
    assert "please" not in matched
    assert "the" not in matched
