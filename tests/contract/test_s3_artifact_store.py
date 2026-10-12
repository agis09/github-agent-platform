"""Contract: S3-compatible ArtifactStore.

Uses a localhost HTTP server that mimics the S3 REST API (PUT/GET) so the
SigV4 signing, hash verification, and error mapping run offline. The
corruption case (stored bytes != digest) is the key assertion.
"""

import hashlib
import http.server
import threading
from collections.abc import Iterator

import pytest

from agent_platform.adapters.artifacts.s3 import S3ArtifactStore, S3Config
from agent_platform.domain.models import Artifact, ArtifactRef
from agent_platform.errors import (
    ArtifactCorruptionError,
    ArtifactNotFoundError,
    ArtifactSizeLimitError,
)


class _FakeS3Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FakeS3/1.0"
    objects: dict[str, bytes] = {}
    should_return: int = 200

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_PUT(self) -> None:  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.should_return != 200:
            self.send_response(self.should_return)
            self.end_headers()
            return
        self.objects[self.path] = body
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        body = self.objects.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        if self.should_return != 200:
            self.send_response(self.should_return)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_s3() -> Iterator[tuple[S3ArtifactStore, type[_FakeS3Handler]]]:
    handler = type("Handler", (_FakeS3Handler,), {"objects": {}, "should_return": 200})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config = S3Config(
            endpoint_url=f"http://127.0.0.1:{server.server_port}",
            bucket="test-bucket",
            access_key_id="test-key",
            secret_access_key="test-secret",
            region="us-east-1",
        )
        yield S3ArtifactStore(config, max_size=1024 * 1024), handler
    finally:
        server.shutdown()
        server.server_close()


def test_put_and_get_roundtrip(fake_s3):
    store, _ = fake_s3
    artifact = Artifact(data=b"hello artifact store")
    ref = store.put(artifact)
    loaded = store.get(ref)
    assert loaded.data == b"hello artifact store"
    assert ref.digest == hashlib.sha256(b"hello artifact store").hexdigest()


def test_get_reverifies_hash(fake_s3):
    """Key assertion: stored bytes tampered after put -> corruption error."""
    store, handler = fake_s3
    ref = store.put(Artifact(data=b"important bytes"))
    key = f"/test-bucket/artifacts/{ref.digest[0:2]}/{ref.digest[2:4]}/{ref.digest}"
    # Tamper server-side (simulates corruption after put).
    handler.objects[key] = b"TAMPERED"
    with pytest.raises(ArtifactCorruptionError, match="corruption"):
        store.get(ref)


def test_get_missing_404(fake_s3):
    store, _ = fake_s3
    with pytest.raises(ArtifactNotFoundError, match="missing"):
        store.get(ArtifactRef(uri="artifact://" + "0" * 64))


def test_size_limit(fake_s3):
    store, _ = fake_s3
    big = b"x" * (1024 * 1024 + 1)
    with pytest.raises(ArtifactSizeLimitError, match="exceeds limit"):
        store.put(Artifact(data=big))


def test_put_is_idempotent_same_digest(fake_s3):
    store, handler = fake_s3
    artifact = Artifact(data=b"same content")
    ref1 = store.put(artifact)
    ref2 = store.put(artifact)
    assert ref1.uri == ref2.uri
    key = f"/test-bucket/artifacts/{ref1.digest[0:2]}/{ref1.digest[2:4]}/{ref1.digest}"
    # Content identical — no corruption from double-put.
    assert hashlib.sha256(handler.objects[key]).hexdigest() == ref1.digest


def test_non_https_endpoint_refused():
    with pytest.raises(ValueError, match="non-HTTPS"):
        S3Config(
            endpoint_url="http://minio.internal:9000",
            bucket="b", access_key_id="k", secret_access_key="s",
        )
