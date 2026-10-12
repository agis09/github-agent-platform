"""Repository candidate proposal: deterministic, LLM-free.

Given the parent (Tasks) issue text and the repositories visible to the
Reader, propose candidate repositories by keyword overlap. The proposal is
a SUGGESTION only — a human still approves each dag edge (the target
repositories and their dependencies are human-approved).
"""

import re

from pydantic import BaseModel, ConfigDict, Field


class CandidateRepo(BaseModel):
    """One suggested repository with the matched keywords as evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    matched_keywords: list[str]
    score: int = Field(ge=0)


def propose_candidates(
    *,
    issue_text: str,
    repositories: list[str],
    max_candidates: int = 5,
) -> list[CandidateRepo]:
    """Rank repositories by keyword overlap with the issue text.

    Keywords are extracted from the issue text (alphanumeric + CJK runs,
    length >= 3, stop words removed) and matched against the repository
    name and its path segments. No network, no LLM: the caller supplies
    the repository list from the Reader.
    """
    text = issue_text.lower()
    words = {
        w for w in re.findall(r"[a-z0-9_]{3,}|[぀-ヿ一-鿿]{2,}", text)
        if w not in _STOPWORDS
    }
    candidates: list[CandidateRepo] = []
    for repository in repositories:
        lowered = repository.lower()
        segments = set(re.split(r"[-_/]", lowered))
        name_tokens = {lowered, *segments}
        matched = sorted(words & name_tokens)
        if matched:
            candidates.append(
                CandidateRepo(
                    repository=repository,
                    matched_keywords=matched,
                    score=len(matched),
                )
            )
    candidates.sort(key=lambda c: (-c.score, c.repository))
    return candidates[:max_candidates]


_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "about",
    "please", "want", "need", "should", "would", "could", "have", "has",
    "are", "was", "were", "will", "can", "not", "you", "your", "our",
})
