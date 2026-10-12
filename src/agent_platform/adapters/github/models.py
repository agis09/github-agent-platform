"""Data models for GitHub read access.

Untrusted content: issue title/body come from the GitHub API and are treated
as untrusted input downstream (prompt builder wraps them in tags). These
models validate shape only — no content filtering happens here.
"""

from pydantic import BaseModel, ConfigDict, Field

from agent_platform.domain.models import GIT_SHA_PATTERN


class IssueSnapshot(BaseModel):
    """Issue content fetched by GitHubReader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    title: str
    body: str | None
    state: str


class IssueComment(BaseModel):
    """One issue comment (untrusted body). The answer flow reads the
    `/agent-platform answer` replies from these."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(ge=1)
    author: str
    body: str


class RepositoryContext(BaseModel):
    """Repository state needed to plan a task: default branch + its HEAD SHA."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    default_branch: str
    head_sha: str = Field(pattern=GIT_SHA_PATTERN.pattern)
