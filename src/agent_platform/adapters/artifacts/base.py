"""ArtifactStore protocol.

Content addressing: the reference IS the digest. Metadata (size, retention
class) lives in the database, not in the store — the store handles bytes and
integrity only.
"""

from typing import Protocol

from agent_platform.domain.models import Artifact, ArtifactRef


class ArtifactStore(Protocol):
    def put(self, artifact: Artifact) -> ArtifactRef: ...

    def get(self, ref: ArtifactRef) -> Artifact: ...

    def delete(self, ref: ArtifactRef) -> None:
        """Remove one artifact. Must raise if the blob is missing — silent
        deletes would hide store/DB divergence."""
        ...
