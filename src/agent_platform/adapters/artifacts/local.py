"""Content-addressed local filesystem artifact store.

- path = <root>/<d[0:2]>/<d[2:4]>/<digest> (two-level fanout)
- atomic write via tmp file + os.replace; same-content put is a no-op (idempotent)
- get() ALWAYS re-verifies the content hash — corruption raises, never hides
- minimal permissions: directories 0700, files 0600 (payloads include
  untrusted issue bodies and LLM I/O)
"""

import hashlib
import os
from pathlib import Path

from agent_platform.domain.models import Artifact, ArtifactRef
from agent_platform.errors import (
    ArtifactCorruptionError,
    ArtifactNotFoundError,
    ArtifactSizeLimitError,
)


class LocalFsArtifactStore:
    def __init__(self, root: Path | str, max_size: int) -> None:
        self._root = Path(root)
        self._max_size = max_size

    def _path_for(self, digest: str) -> Path:
        return self._root / digest[0:2] / digest[2:4] / digest

    def put(self, artifact: Artifact) -> ArtifactRef:
        if len(artifact.data) > self._max_size:
            msg = f"artifact size {len(artifact.data)} exceeds limit {self._max_size}"
            raise ArtifactSizeLimitError(msg)
        digest = hashlib.sha256(artifact.data).hexdigest()
        path = self._path_for(digest)
        if not path.exists():
            # Explicit mode bits: mkdir's mode argument is masked by umask.
            self._root.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(self._root, 0o700)
            os.chmod(path.parent, 0o700)
            tmp = path.with_name(f".tmp-{os.getpid()}-{digest[0:8]}")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(artifact.data)
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    tmp.unlink()
        return ArtifactRef(uri=f"artifact://{digest}")

    def delete(self, ref: ArtifactRef) -> None:
        path = self._path_for(ref.digest)
        if not path.exists():
            msg = f"artifact missing: {ref.uri}"
            raise ArtifactNotFoundError(msg)
        path.unlink()

    def get(self, ref: ArtifactRef) -> Artifact:
        path = self._path_for(ref.digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            msg = f"artifact missing: {ref.uri}"
            raise ArtifactNotFoundError(msg) from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != ref.digest:
            msg = f"artifact corruption: {ref.uri} (content hash {actual})"
            raise ArtifactCorruptionError(msg)
        return Artifact(data=data)
