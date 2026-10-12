"""Contract: `artifacts gc` deletes expired ephemeral artifacts from the
store AND the metadata table, keeps audit-class rows, and surfaces store
failures as a non-zero exit."""

import sqlite3

import pytest

from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.cli import main as cli_main
from agent_platform.domain.models import Artifact


def _seed(home, store, *, digest_data: bytes, retention_class: str, created_at: str):
    ref = store.put(Artifact(data=digest_data))
    conn = sqlite3.connect(home / "state.sqlite3")
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO artifacts (digest, size, retention_class, created_at)"
            " VALUES (?, ?, ?, ?)",
            (ref.digest, len(digest_data), retention_class, created_at),
        )
    conn.close()
    return ref


def _make_home(tmp_path):
    home = tmp_path / "gc-home"
    home.mkdir()
    SqliteTaskRepository = pytest.importorskip(
        "agent_platform.orchestrator.repository"
    ).SqliteTaskRepository
    SqliteTaskRepository(home / "state.sqlite3")  # creates schema v7
    return home


def test_gc_deletes_expired_ephemeral_and_keeps_audit_and_fresh(tmp_path):
    from agent_platform.orchestrator.repository import SqliteTaskRepository

    home = tmp_path / "gc-home"
    home.mkdir()
    SqliteTaskRepository(home / "state.sqlite3")
    store = LocalFsArtifactStore(home / "artifacts", max_size=1024)

    old_ephemeral = _seed(home, store, digest_data=b"old-ephemeral",
                          retention_class="ephemeral", created_at="2026-01-01T00:00:00Z")
    fresh_ephemeral = _seed(home, store, digest_data=b"fresh-ephemeral",
                            retention_class="ephemeral", created_at="2099-01-01T00:00:00Z")
    old_audit = _seed(home, store, digest_data=b"old-audit",
                      retention_class="audit", created_at="2026-01-01T00:00:00Z")

    rc = cli_main(["artifacts", "gc", "--retention-days", "30",
                   "--home", str(home)])
    assert rc == 0

    # old ephemeral: gone from store and DB
    from agent_platform.errors import ArtifactNotFoundError

    with pytest.raises(ArtifactNotFoundError):
        store.get(old_ephemeral)
    # fresh ephemeral: intact
    assert store.get(fresh_ephemeral).data == b"fresh-ephemeral"
    # old audit: intact (gc only collects the requested class)
    assert store.get(old_audit).data == b"old-audit"

    conn = sqlite3.connect(home / "state.sqlite3")
    remaining = {r[0] for r in conn.execute("SELECT digest FROM artifacts").fetchall()}
    conn.close()
    assert old_ephemeral.digest not in remaining
    assert fresh_ephemeral.digest in remaining
    assert old_audit.digest in remaining


def test_gc_audit_class_requires_explicit_confirmation(tmp_path):
    home = _make_home(tmp_path)
    rc = cli_main(["artifacts", "gc", "--retention-days", "30",
                   "--retention-class", "audit", "--home", str(home)])
    assert rc != 0  # refused without AGENT_PLATFORM_GC_AUDIT_CONFIRM
