"""S3-compatible ArtifactStore: remote persistence for audit records
(request/response, commands, diffs, test results recorded to an
internal store only).

Design:
- AWS SigV4 request signing implemented in stdlib (hmac/hashlib) — no
  boto3 dependency, the signing is small and auditable.
- put/get use the S3 REST API against a MinIO (kind) or internal object
  storage endpoint. HTTPS enforced; http allowed only for loopback (kind
  testing).
- Credential handling: the store itself runs in the Orchestrator process.
  The Worker receives a short-lived presigned PUT URL per artifact via the
  Job env — the Worker never holds long-lived credentials (same principle
  as the GitHub credential separation).
- get() re-verifies the SHA-256 content hash like LocalFsArtifactStore —
  corruption raises, never hides.
"""

import datetime
import hashlib
import hmac
import urllib.error
import urllib.request

from agent_platform.domain.models import Artifact, ArtifactRef
from agent_platform.errors import (
    ArtifactCorruptionError,
    ArtifactNotFoundError,
    ArtifactSizeLimitError,
)


class S3Config:
    """Non-secret S3 endpoint settings. The secret key name resolves at
    call time from the environment (never stored here)."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
    ) -> None:
        _require_https_or_loopback(endpoint_url)
        self.endpoint_url = endpoint_url.rstrip("/")
        self.bucket = bucket
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.region = region


def _require_https_or_loopback(url: str) -> str:
    if url.startswith("https://"):
        return url
    if "://127.0.0.1:" in url or "://localhost:" in url:
        return url  # kind / MinIO dev testing
    msg = f"refusing non-HTTPS object storage endpoint: {url}"
    raise ValueError(msg)


# --- AWS SigV4 (stdlib only) ---


def _sigv4_key(secret: str, date: str, region: str, service: str) -> bytes:
    k_date = hmac.new(f"AWS4{secret}".encode(), date.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _sigv4_headers(
    method: str,
    url_path: str,
    *,
    host: str,
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
) -> dict[str, str]:
    now = datetime.datetime.now(datetime.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    canonical_headers = (
        f"host:{host}\\nx-amz-content-sha256:{payload_hash}\\n"
        f"x-amz-date:{amz_date}\\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = (
        f"{method}\\n{url_path}\\n\\n{canonical_headers}\\n{signed_headers}\\n"
        f"{payload_hash}"
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\\n{amz_date}\\n{scope}\\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    signing_key = _sigv4_key(secret_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }


def presign_put_url(
    *, config: S3Config, key: str, expires_seconds: int = 900
) -> str:
    """Presigned PUT URL for the Worker: short-lived, single-object write."""
    import urllib.parse

    host = _host_of(config.endpoint_url)
    url_path = f"/{config.bucket}/{key}"
    now = datetime.datetime.now(datetime.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = f"{date_stamp}/{config.region}/s3/aws4_request"
    # UNSIGNED-PAYLOAD: the worker streams the body without hashing.
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{config.access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires_seconds),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = urllib.parse.urlencode(query, safe="/")
    canonical_request = (
        f"PUT\\n{url_path}\\n{canonical_query}\\nhost:{host}\\n\\nhost\\nUNSIGNED-PAYLOAD"
    )
    string_to_sign = (
        f"AWS4-HMAC-SHA256\\n{amz_date}\\n{scope}\\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    signing_key = _sigv4_key(config.secret_access_key, date_stamp, config.region, "s3")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"{config.endpoint_url}{url_path}?{canonical_query}"
        f"&X-Amz-Signature={signature}"
    )


def presign_get_url(
    *, config: S3Config, key: str, expires_seconds: int = 900
) -> str:
    """Presigned GET URL for short-lived, single-object reads."""
    host = _host_of(config.endpoint_url)
    url_path = f"/{config.bucket}/{key}"
    now = datetime.datetime.now(datetime.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = f"{date_stamp}/{config.region}/s3/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{config.access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires_seconds),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = urllib.parse.urlencode(query, safe="/")
    canonical_request = (
        f"GET\n{url_path}\n{canonical_query}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD"
    )
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    signing_key = _sigv4_key(config.secret_access_key, date_stamp, config.region, "s3")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"{config.endpoint_url}{url_path}?{canonical_query}"
        f"&X-Amz-Signature={signature}"
    )


def _host_of(endpoint_url: str) -> str:
    return endpoint_url.split("//", 1)[1].split("/", 1)[0]


class S3ArtifactStore:
    """ArtifactStore implementation over an S3-compatible object store.

    Same contract as LocalFsArtifactStore: put is idempotent, get ALWAYS
    re-verifies the SHA-256 content hash — corruption raises, never hides.
    """

    def __init__(self, config: S3Config, *, max_size: int) -> None:
        _require_https_or_loopback(config.endpoint_url)
        self._config = config
        self._max_size = max_size

    def put(self, artifact: Artifact) -> ArtifactRef:
        if len(artifact.data) > self._max_size:
            msg = f"artifact size {len(artifact.data)} exceeds limit {self._max_size}"
            raise ArtifactSizeLimitError(msg)
        digest = hashlib.sha256(artifact.data).hexdigest()
        key = f"artifacts/{digest[0:2]}/{digest[2:4]}/{digest}"
        url = (
            f"{self._config.endpoint_url}/{self._config.bucket}/{key}"
        )
        payload_hash = hashlib.sha256(artifact.data).hexdigest()
        host = _host_of(self._config.endpoint_url)
        headers = _sigv4_headers(
            "PUT", f"/{self._config.bucket}/{key}",
            host=host, payload_hash=payload_hash,
            access_key=self._config.access_key_id,
            secret_key=self._config.secret_access_key,
            region=self._config.region,
        )
        request = urllib.request.Request(  # noqa: S310 — https enforced
            url, data=artifact.data, method="PUT", headers=headers,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 — https enforced
                request, timeout=60
            ) as resp:
                if resp.status not in (200, 201):
                    msg = f"object store PUT returned {resp.status}"
                    raise ArtifactNotFoundError(msg)
        except urllib.error.HTTPError as exc:
            msg = f"object store PUT failed: {exc.code} {exc.reason}"
            raise ArtifactCorruptionError(msg) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"object store unreachable: {exc}"
            raise ArtifactCorruptionError(msg) from exc
        return ArtifactRef(uri=f"artifact://{digest}")

    def delete(self, ref: ArtifactRef) -> None:
        digest = ref.digest
        key = f"artifacts/{digest[0:2]}/{digest[2:4]}/{digest}"
        url = f"{self._config.endpoint_url}/{self._config.bucket}/{key}"
        headers = _sigv4_headers(
            "DELETE", f"/{self._config.bucket}/{key}",
            host=_host_of(self._config.endpoint_url),
            payload_hash=hashlib.sha256(b"").hexdigest(),
            access_key=self._config.access_key_id,
            secret_key=self._config.secret_access_key,
            region=self._config.region,
        )
        request = urllib.request.Request(  # noqa: S310 — https enforced
            url, method="DELETE", headers=headers,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 — https enforced
                request, timeout=60
            ) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                msg = f"artifact missing: {ref.uri}"
                raise ArtifactNotFoundError(msg) from exc
            msg = f"object store DELETE failed: {exc.code} {exc.reason}"
            raise ArtifactCorruptionError(msg) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"object store unreachable: {exc}"
            raise ArtifactCorruptionError(msg) from exc

    def get(self, ref: ArtifactRef) -> Artifact:
        digest = ref.digest
        key = f"artifacts/{digest[0:2]}/{digest[2:4]}/{digest}"
        url = f"{self._config.endpoint_url}/{self._config.bucket}/{key}"
        headers = _sigv4_headers(
            "GET", f"/{self._config.bucket}/{key}",
            host=_host_of(self._config.endpoint_url),
            payload_hash=hashlib.sha256(b"").hexdigest(),
            access_key=self._config.access_key_id,
            secret_key=self._config.secret_access_key,
            region=self._config.region,
        )
        request = urllib.request.Request(  # noqa: S310 — https enforced
            url, method="GET", headers=headers,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 — https enforced
                request, timeout=60
            ) as resp:
                data: bytes = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                msg = f"artifact missing: {ref.uri}"
                raise ArtifactNotFoundError(msg) from exc
            msg = f"object store GET failed: {exc.code} {exc.reason}"
            raise ArtifactCorruptionError(msg) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"object store unreachable: {exc}"
            raise ArtifactCorruptionError(msg) from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            msg = f"artifact corruption: {ref.uri} (content hash {actual})"
            raise ArtifactCorruptionError(msg)
        return Artifact(data=data)
