"""Contract artifacts: repository-to-repository agreements stored as
content-addressed artifacts.

A contract change is published as an artifact first; consumers then
reference that version.
A contract has a name (e.g. "todos-api"), a semantic version, the schema
payload, and a set of consumer repositories. Consumers pin an exact
version — a contract change is published as a NEW version, never mutated
in place (content addressing enforces this: same bytes = same digest).

The parent task's aggregation surfaces these references so a human can
trace 契約、PR、test、残課題 from the parent issue.
"""

from pydantic import BaseModel, ConfigDict, Field

from agent_platform.domain.models import GIT_SHA_PATTERN


class ContractArtifact(BaseModel):
    """A published contract version. Immutable once stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="1", pattern=r"^1$")
    name: str = Field(min_length=1, max_length=128,
                      pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str = Field(min_length=1, max_length=64,
                         pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
    publisher_task_id: str = Field(min_length=1)
    payload: str = Field(min_length=1, max_length=1_000_000)  # JSON/YAML text
    consumers: list[str] = Field(default_factory=list)  # repo names

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization for content addressing."""
        import json
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")


class ContractRef(BaseModel):
    """A consumer's pin on a contract version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_name: str = Field(min_length=1, max_length=128,
                               pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str = Field(min_length=1, max_length=64,
                         pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
    digest: str = Field(pattern=GIT_SHA_PATTERN.pattern)
    consumer_task_id: str = Field(min_length=1)
