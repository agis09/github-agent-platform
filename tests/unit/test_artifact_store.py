"""LocalFsArtifactStore: content addressing, atomicity, corruption detection,
size limits, permissions."""

import stat

import pytest

from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.domain.models import Artifact, ArtifactRef
from agent_platform.errors import (
    ArtifactCorruptionError,
    ArtifactNotFoundError,
    ArtifactSizeLimitError,
)


@pytest.fixture
def store(tmp_path):
    return LocalFsArtifactStore(tmp_path / "artifacts", max_size=1024)


def test_put_get_roundtrip(store):
    ref = store.put(Artifact(data=b"hello"))
    assert store.get(ref).data == b"hello"


def test_same_content_same_ref(store):
    a = store.put(Artifact(data=b"same"))
    b = store.put(Artifact(data=b"same"))
    assert a.uri == b.uri


def test_ref_is_sha256_of_content(store):
    import hashlib

    ref2 = store.put(Artifact(data=b"determinism"))
    assert ref2.uri == f"artifact://{hashlib.sha256(b'determinism').hexdigest()}"


def test_corruption_detected(store):
    ref = store.put(Artifact(data=b"integrity"))
    # tamper with the file bytes
    path = store._path_for(ref.digest)
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactCorruptionError):
        store.get(ref)


def test_missing_artifact_raises(store):
    ref = ArtifactRef(uri=f"artifact://{'a' * 64}")
    with pytest.raises(ArtifactNotFoundError):
        store.get(ref)


def test_size_limit(store):
    with pytest.raises(ArtifactSizeLimitError):
        store.put(Artifact(data=b"x" * 2048))


def test_no_tmp_files_left_behind(store):
    store.put(Artifact(data=b"cleanup"))
    remaining = [p.name for p in store._root.rglob("*") if ".tmp-" in p.name]
    assert remaining == []


def test_permissions(store):
    ref = store.put(Artifact(data=b"perms"))
    path = store._path_for(ref.digest)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    dir_mode = stat.S_IMODE(path.parent.parent.parent.stat().st_mode)
    assert dir_mode & 0o077 == 0  # no group/other bits


def test_delete_removes_blob_and_raises_when_missing(tmp_path):

    from agent_platform.adapters.artifacts.base import ArtifactStore
    from agent_platform.domain.models import Artifact

    store = LocalFsArtifactStore(tmp_path, max_size=1024)
    # Protocol conformance: delete is part of the store contract (artifacts gc).
    assert hasattr(type(store), "delete") or isinstance(store, ArtifactStore)
    ref = store.put(Artifact(data=b"to-be-deleted"))
    store.delete(ref)
    from agent_platform.errors import ArtifactNotFoundError

    with pytest.raises(ArtifactNotFoundError):
        store.get(ref)


def test_delete_missing_raises(tmp_path):
    from agent_platform.adapters.artifacts.local import ArtifactNotFoundError
    from agent_platform.domain.models import ArtifactRef

    store = LocalFsArtifactStore(tmp_path, max_size=1024)
    missing = ArtifactRef(uri=f"artifact://{'f' * 64}")
    with pytest.raises(ArtifactNotFoundError):
        store.delete(missing)
