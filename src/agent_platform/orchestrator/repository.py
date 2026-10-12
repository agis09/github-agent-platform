"""SQLite task repository.

- BEGIN IMMEDIATE transactions for CAS state transitions
- schema_version gate: no auto-migration; unsupported version refuses to open
- UNIQUE(repository, issue_number) and UNIQUE(task_id, plan_hash) are the DB
  backstops for idempotency rules
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_platform.domain.models import (
    Approval,
    Task,
    TaskStatus,
)
from agent_platform.domain.state_machine import TransitionOutcome, can_transition
from agent_platform.errors import (
    NotFoundError,
    StateConflictError,
    UnsupportedSchemaError,
)

SCHEMA_VERSION = "8"


def _loads(raw: str) -> list[str]:
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        msg = f"expected JSON array, got {type(parsed).__name__}"
        raise ValueError(msg)
    return [str(item) for item in parsed]

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id      TEXT PRIMARY KEY,
  repository   TEXT    NOT NULL,
  issue_number INTEGER NOT NULL CHECK (issue_number >= 1),
  revision     INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  search_forbidden_tokens TEXT NOT NULL DEFAULT '[]',
  status       TEXT    NOT NULL CHECK (status IN
    ('RECEIVED','PLANNING','AWAITING_APPROVAL','APPROVED','EXECUTING',
     'READY_TO_PUBLISH','PR_CREATED','REVIEWING','COMPLETED','FAILED',
     'CANCELLED','NEEDS_REAPPROVAL')),
  base_branch  TEXT,
  base_sha     TEXT,
  plan_hash    TEXT,
  execution_ref TEXT,
  created_at   TEXT    NOT NULL,
  updated_at   TEXT    NOT NULL,
  UNIQUE (repository, issue_number, revision)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS approvals (
  approval_id   TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(task_id),
  plan_hash     TEXT NOT NULL,
  repository    TEXT NOT NULL,
  base_branch   TEXT NOT NULL,
  base_sha      TEXT NOT NULL,
  allowed_paths TEXT NOT NULL,
  test_commands TEXT NOT NULL,
  search_forbidden_tokens TEXT NOT NULL DEFAULT '[]',
  approver      TEXT NOT NULL,
  approved_at   TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  UNIQUE (task_id, plan_hash)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  key        TEXT PRIMARY KEY,
  operation  TEXT NOT NULL CHECK (operation IN ('plan','approve','reject','execute')),
  task_id    TEXT NOT NULL REFERENCES tasks(task_id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_dag_edges (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_task_id TEXT NOT NULL REFERENCES tasks(task_id),
  child_task_id  TEXT NOT NULL REFERENCES tasks(task_id),
  created_by   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  UNIQUE (parent_task_id, child_task_id),
  CHECK (parent_task_id != child_task_id)
);

CREATE TABLE IF NOT EXISTS contract_versions (
  name           TEXT NOT NULL CHECK (name GLOB '[a-z0-9]*' AND length(name) <= 128),
  version        TEXT NOT NULL CHECK (length(version) <= 64),
  publisher_task_id TEXT NOT NULL REFERENCES tasks(task_id),
  digest         TEXT NOT NULL CHECK (length(digest) = 64),
  created_at     TEXT NOT NULL,
  PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS consumer_pins (
  contract_name  TEXT NOT NULL,
  version        TEXT NOT NULL,
  digest         TEXT NOT NULL CHECK (length(digest) = 64),
  consumer_task_id TEXT NOT NULL REFERENCES tasks(task_id),
  created_at     TEXT NOT NULL,
  PRIMARY KEY (contract_name, consumer_task_id),
  FOREIGN KEY (contract_name, version)
    REFERENCES contract_versions(name, version)
);

CREATE TABLE IF NOT EXISTS artifacts (
  digest          TEXT PRIMARY KEY CHECK (length(digest) = 64),
  size            INTEGER NOT NULL CHECK (size >= 0),
  retention_class TEXT NOT NULL CHECK (retention_class IN ('audit','ephemeral')),
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL REFERENCES tasks(task_id),
  from_status TEXT NOT NULL,
  to_status   TEXT NOT NULL,
  reason      TEXT,
  occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);
"""


class SqliteTaskRepository:
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._init_schema()

    def _init_schema(self) -> None:
        # executescript commits implicitly; schema creation precedes any
        # transactional use, and the version gate below runs in its own tx.
        self._conn.executescript(_DDL)
        with self._tx() as cur:
            row = cur.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            elif row["value"] != SCHEMA_VERSION:
                msg = (
                    f"database schema version {row['value']!r} is not supported "
                    f"(expected {SCHEMA_VERSION!r}); auto-migration is not implemented"
                )
                raise UnsupportedSchemaError(msg)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # --- tasks ---

    def get_task(self, task_id: str) -> Task:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            msg = f"task not found: {task_id}"
            raise NotFoundError(msg)
        return self._task_from_row(row)

    def get_task_by_issue(self, repository: str, issue_number: int) -> Task | None:
        """Latest revision for the issue (redo creates revisions > 1)."""
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE repository = ? AND issue_number = ?"
            " ORDER BY revision DESC LIMIT 1",
            (repository, issue_number),
        ).fetchone()
        if row is None:
            return None
        return self._task_from_row(row)

    def _task_from_row(self, row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            repository=row["repository"],
            issue_number=row["issue_number"],
            revision=row["revision"],
            search_forbidden_tokens=_loads(row["search_forbidden_tokens"]),
            status=TaskStatus(row["status"]),
            base_branch=row["base_branch"],
            base_sha=row["base_sha"],
            plan_hash=row["plan_hash"],
            execution_ref=row["execution_ref"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def latest_revision(self, repository: str, issue_number: int) -> int:
        row = self._conn.execute(
            "SELECT MAX(revision) AS rev FROM tasks"
            " WHERE repository = ? AND issue_number = ?",
            (repository, issue_number),
        ).fetchone()
        return int(row["rev"]) if row is not None and row["rev"] else 0

    def insert_task(self, task: Task) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO tasks (task_id, repository, issue_number, revision,"
                " search_forbidden_tokens, status, base_branch, base_sha,"
                " plan_hash, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    task.repository,
                    task.issue_number,
                    task.revision,
                    json.dumps(task.search_forbidden_tokens, separators=(",", ":")),
                    task.status.value,
                    task.base_branch,
                    task.base_sha,
                    task.plan_hash,
                    task.created_at,
                    task.updated_at,
                ),
            )

    def update_task_plan(
        self,
        task_id: str,
        *,
        plan_hash: str,
        base_branch: str,
        base_sha: str,
        search_forbidden_tokens: list[str] | None = None,
        to_status: str,
        now: str,
        reason: str = "plan recorded",
    ) -> None:
        """Bind plan result fields and move to AWAITING_APPROVAL atomically."""
        with self._tx() as cur:
            row = cur.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                msg = f"task not found: {task_id}"
                raise NotFoundError(msg)
            current = row["status"]
            if current != TaskStatus.PLANNING.value:
                msg = f"task {task_id} is {current}, expected PLANNING"
                raise StateConflictError(msg)
            cur.execute(
                "UPDATE tasks SET plan_hash = ?, base_branch = ?, base_sha = ?,"
                " search_forbidden_tokens = ?, status = ?, updated_at = ?"
                " WHERE task_id = ? AND status = ?",
                (
                    plan_hash,
                    base_branch,
                    base_sha,
                    json.dumps(search_forbidden_tokens or [], separators=(",", ":")),
                    to_status,
                    now,
                    task_id,
                    current,
                ),
            )
            cur.execute(
                "INSERT INTO task_events (task_id, from_status, to_status, reason, occurred_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, current, to_status, reason, now),
            )

    def update_task_execution(
        self,
        task_id: str,
        *,
        execution_ref: str,
        to_status: str,
        now: str,
        reason: str | None = None,
    ) -> None:
        """Bind the execution artifact reference and move state atomically."""
        with self._tx() as cur:
            row = cur.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                msg = f"task not found: {task_id}"
                raise NotFoundError(msg)
            current = row["status"]
            if current != "EXECUTING":
                msg = f"task {task_id} is {current}, expected EXECUTING"
                raise StateConflictError(msg)
            cur.execute(
                "UPDATE tasks SET execution_ref = ?, status = ?, updated_at = ?"
                " WHERE task_id = ? AND status = ?",
                (execution_ref, to_status, now, task_id, current),
            )
            cur.execute(
                "INSERT INTO task_events (task_id, from_status, to_status, reason, occurred_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, current, to_status, reason, now),
            )

    def transition_task(
        self, task_id: str, to_status: str, *, reason: str | None = None, now: str
    ) -> TransitionOutcome:
        """CAS transition. Same-state is an idempotent replay, not an error."""
        with self._tx() as cur:
            row = cur.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                msg = f"task not found: {task_id}"
                raise NotFoundError(msg)
            current = row["status"]
            if current == to_status:
                return TransitionOutcome.ALREADY_APPLIED
            if not can_transition(current, to_status):
                msg = f"cannot transition {current} -> {to_status}"
                raise StateConflictError(msg)
            cur.execute(
                "UPDATE tasks SET status = ?, updated_at = ?"
                " WHERE task_id = ? AND status = ?",
                (to_status, now, task_id, current),
            )
            cur.execute(
                "INSERT INTO task_events (task_id, from_status, to_status, reason, occurred_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, current, to_status, reason, now),
            )
            return TransitionOutcome.APPLIED

    def list_events(self, task_id: str) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT from_status, to_status, reason, occurred_at FROM task_events"
            " WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [
            {
                "from": r["from_status"],
                "to": r["to_status"],
                "reason": r["reason"] or "",
                "at": r["occurred_at"],
            }
            for r in rows
        ]

    # --- approvals ---

    def find_approval(self, task_id: str, plan_hash: str) -> Approval | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? AND plan_hash = ?",
            (task_id, plan_hash),
        ).fetchone()
        if row is None:
            return None
        return Approval(
            approval_id=row["approval_id"],
            task_id=row["task_id"],
            plan_hash=row["plan_hash"],
            repository=row["repository"],
            base_branch=row["base_branch"],
            base_sha=row["base_sha"],
            allowed_paths=_loads(row["allowed_paths"]),
            test_commands=_loads(row["test_commands"]),
            search_forbidden_tokens=_loads(row["search_forbidden_tokens"]),
            approver=row["approver"],
            approved_at=row["approved_at"],
            expires_at=row["expires_at"],
        )

    def insert_approval(self, approval: Approval) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO approvals (approval_id, task_id, plan_hash, repository,"
                " base_branch, base_sha, allowed_paths, test_commands,"
                " search_forbidden_tokens, approver, approved_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval.approval_id,
                    approval.task_id,
                    approval.plan_hash,
                    approval.repository,
                    approval.base_branch,
                    approval.base_sha,
                    json.dumps(approval.allowed_paths, separators=(",", ":")),
                    json.dumps(approval.test_commands, separators=(",", ":")),
                    json.dumps(approval.search_forbidden_tokens, separators=(",", ":")),
                    approval.approver,
                    approval.approved_at,
                    approval.expires_at,
                ),
            )

    # --- idempotency keys ---

    def get_idempotency(self, key: str) -> dict[str, str] | None:
        row = self._conn.execute(
            "SELECT key, operation, task_id, created_at FROM idempotency_keys"
            " WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "key": row["key"],
            "operation": row["operation"],
            "task_id": row["task_id"],
            "created_at": row["created_at"],
        }

    def insert_idempotency(
        self, key: str, operation: str, task_id: str, now: str, *, ignore: bool = False
    ) -> None:
        prefix = "INSERT OR IGNORE" if ignore else "INSERT"
        # prefix is a fixed literal chosen above, not user input.
        stmt = (
            f"{prefix} INTO idempotency_keys (key, operation, task_id, created_at)"
            " VALUES (?, ?, ?, ?)"
        )
        with self._tx() as cur:
            try:
                cur.execute(stmt, (key, operation, task_id, now))
            except sqlite3.IntegrityError as exc:
                msg = f"idempotency key already bound: {key}"
                raise StateConflictError(msg) from exc

    # --- artifacts metadata ---

    # --- contracts ---

    def insert_contract_version(
        self, *, name: str, version: str, publisher_task_id: str,
        digest: str, now: str,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO contract_versions"
                " (name, version, publisher_task_id, digest, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, version, publisher_task_id, digest, now),
            )

    def get_contract_version(
        self, *, name: str, version: str
    ) -> dict[str, str] | None:
        row = self._conn.execute(
            "SELECT name, version, publisher_task_id, digest, created_at"
            " FROM contract_versions WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"], "version": row["version"],
            "publisher_task_id": row["publisher_task_id"],
            "digest": row["digest"], "created_at": row["created_at"],
        }

    def list_contract_versions(self, *, name: str) -> list[dict[str, str]]:
        """All versions of a contract, newest first (created_at desc)."""
        rows = self._conn.execute(
            "SELECT name, version, publisher_task_id, digest, created_at"
            " FROM contract_versions WHERE name = ? ORDER BY created_at DESC",
            (name,),
        ).fetchall()
        return [
            {
                "name": r["name"], "version": r["version"],
                "publisher_task_id": r["publisher_task_id"],
                "digest": r["digest"], "created_at": r["created_at"],
            }
            for r in rows
        ]

    def insert_consumer_pin(
        self, *, contract_name: str, version: str, digest: str,
        consumer_task_id: str, now: str,
    ) -> None:
        """Pin a consumer to an exact contract version. Re-pinning the same
        (contract, consumer) updates the row — a consumer migrating to a new
        version replaces its pin, and the change is visible in the audit."""
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO consumer_pins"
                " (contract_name, version, digest, consumer_task_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (contract_name, consumer_task_id)"
                " DO UPDATE SET version = excluded.version,"
                " digest = excluded.digest, created_at = excluded.created_at",
                (contract_name, version, digest, consumer_task_id, now),
            )

    def list_consumer_pins_by_publisher(
        self, *, publisher_task_id: str
    ) -> list[dict[str, str]]:
        """Pins of contracts published by the given task (rollback
        material: contracts whose publisher failed)."""
        rows = self._conn.execute(
            "SELECT cp.contract_name, cp.version, cp.digest,"
            " cp.consumer_task_id, cp.created_at"
            " FROM consumer_pins cp"
            " JOIN contract_versions cv"
            " ON cv.name = cp.contract_name AND cv.version = cp.version"
            " WHERE cv.publisher_task_id = ?",
            (publisher_task_id,),
        ).fetchall()
        return [
            {
                "contract_name": r["contract_name"], "version": r["version"],
                "digest": r["digest"], "consumer_task_id": r["consumer_task_id"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def list_consumer_pins(self, *, contract_name: str) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT contract_name, version, digest, consumer_task_id, created_at"
            " FROM consumer_pins WHERE contract_name = ? ORDER BY created_at",
            (contract_name,),
        ).fetchall()
        return [
            {
                "contract_name": r["contract_name"], "version": r["version"],
                "digest": r["digest"], "consumer_task_id": r["consumer_task_id"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # --- DAG edges ---

    def insert_dag_edge(
        self, parent_task_id: str, child_task_id: str, *, created_by: str, now: str
    ) -> None:
        """Record a parent->child dependency. Idempotent (UNIQUE pair);
        self-edges are rejected by the schema CHECK."""
        with self._tx() as cur:
            try:
                cur.execute(
                    "INSERT INTO task_dag_edges"
                    " (parent_task_id, child_task_id, created_by, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (parent_task_id, child_task_id, created_by, now),
                )
            except sqlite3.IntegrityError as exc:
                msg = f"dag edge already bound or invalid: {parent_task_id}->{child_task_id}"
                raise StateConflictError(msg) from exc

    def list_children(self, parent_task_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT child_task_id FROM task_dag_edges WHERE parent_task_id = ?"
            " ORDER BY id",
            (parent_task_id,),
        ).fetchall()
        return [r["child_task_id"] for r in rows]

    def list_dependencies(self, child_task_id: str) -> list[str]:
        """Tasks this child depends on (its parents) — must be complete
        before the child may execute/publish."""
        rows = self._conn.execute(
            "SELECT parent_task_id FROM task_dag_edges WHERE child_task_id = ?"
            " ORDER BY id",
            (child_task_id,),
        ).fetchall()
        return [r["parent_task_id"] for r in rows]

    def insert_artifact_metadata(
        self, digest: str, size: int, retention_class: str, now: str
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO artifacts (digest, size, retention_class, created_at)"
                " VALUES (?, ?, ?, ?)",
                (digest, size, retention_class, now),
            )

    def list_artifact_metadata_older_than(
        self, *, retention_class: str, cutoff: str
    ) -> list[dict[str, Any]]:
        """Artifact metadata rows of one retention class created before the
        cutoff (ISO timestamps compare lexically). Used by `artifacts gc`."""
        rows = self._conn.execute(
            "SELECT digest, size, retention_class, created_at FROM artifacts"
            " WHERE retention_class = ? AND created_at < ?",
            (retention_class, cutoff),
        ).fetchall()
        return [
            {
                "digest": r["digest"],
                "size": r["size"],
                "retention_class": r["retention_class"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def delete_artifact_metadata(self, digest: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM artifacts WHERE digest = ?", (digest,))

    def get_artifact_metadata(self, digest: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT digest, size, retention_class, created_at FROM artifacts"
            " WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            return None
        return {
            "digest": row["digest"],
            "size": row["size"],
            "retention_class": row["retention_class"],
            "created_at": row["created_at"],
        }
