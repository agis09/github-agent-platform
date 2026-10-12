"""Contract: contract artifact publishing, consumer pinning,
immutability, and parent-aggregation traceability."""

import datetime
import hashlib

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.clock import FixedClock
from agent_platform.config import Settings
from agent_platform.contracts import ContractArtifact
from agent_platform.errors import IntegrityError, NotFoundError
from agent_platform.orchestrator.repository import SqliteTaskRepository
from agent_platform.orchestrator.service import TaskService
from tests.conftest import BASE_SHA

UTC = datetime.UTC


def _service(tmp_path):
    settings = Settings(
        home_dir=str(tmp_path),
        artifact_max_size=1024 * 1024,
        default_approval_ttl_hours=72,
        agent_binary="codex",
        agent_model=None,
        agent_base_url=None,
        agent_api_key_env="LLM_API_KEY",
        codex_wire_api="responses",
        agent_timeout_seconds=600.0,
        agent_max_output_tokens=None,
    )
    repo = SqliteTaskRepository(tmp_path / "state.sqlite3")
    return TaskService(
        repository=repo,
        adapter=FakeAgentAdapter(),
        artifacts=LocalFsArtifactStore(tmp_path / "artifacts", 1024 * 1024),
        clock=FixedClock(datetime.datetime(2026, 9, 18, 9, 0, 0, tzinfo=UTC)),
        settings=settings,
    ), repo


def _contract(version="1.0.0", task="task-7704a4cbeffa", consumers=None):
    return ContractArtifact(
        name="todos-api",
        version=version,
        publisher_task_id=task,
        payload='{"endpoint": "/v1/todos", "fields": ["id", "title"]}',
        consumers=consumers or [],
    )


def _ensure_publisher_task(service):
    return service.plan(
        repository="owner/publisher", issue_number=1, base_branch="main",
        base_sha=BASE_SHA,
    ).task.task_id


def test_publish_contract_stores_artifact_with_digest(tmp_path):
    service, repo = _service(tmp_path)
    publisher = _ensure_publisher_task(service)
    contract = _contract(task=publisher)
    outcome = service.publish_contract(contract=contract)
    assert outcome.duplicate is False
    assert outcome.digest == hashlib.sha256(contract.canonical_bytes()).hexdigest()
    # The version is recorded.
    record = repo.get_contract_version(name="todos-api", version="1.0.0")
    assert record is not None
    assert record["digest"] == outcome.digest
    # The artifact round-trips.
    artifact = service._artifacts.get(
        type("R", (), {"uri": f"artifact://{outcome.digest}",
                       "digest": outcome.digest})()
    )
    assert ContractArtifact.model_validate_json(artifact.data).version == "1.0.0"


def test_contract_version_is_immutable(tmp_path):
    """Same (name, version) with different bytes is an integrity error."""
    service, _ = _service(tmp_path)
    publisher = _ensure_publisher_task(service)
    service.publish_contract(contract=_contract(task=publisher))
    tampered = _contract(task=publisher).model_copy(
        update={"payload": '{"endpoint": "/v2/todos"}'}
    )
    with pytest.raises(IntegrityError, match="versions are immutable"):
        service.publish_contract(contract=tampered)


def test_same_bytes_is_idempotent_replay(tmp_path):
    service, _ = _service(tmp_path)
    publisher = _ensure_publisher_task(service)
    first = service.publish_contract(contract=_contract(task=publisher))
    second = service.publish_contract(contract=_contract(task=publisher))
    assert first.duplicate is False
    assert second.duplicate is True


def test_new_version_is_a_separate_artifact(tmp_path):
    """契約変更は先に artifact 化: a new version coexists with the old."""
    service, repo = _service(tmp_path)
    publisher = _ensure_publisher_task(service)
    service.publish_contract(contract=_contract(version="1.0.0", task=publisher))
    service.publish_contract(contract=ContractArtifact(
        name="todos-api", version="1.1.0", publisher_task_id=publisher,
        payload='{"endpoint": "/v1/todos", "fields": ["id", "title", "done"]}',
    ))
    versions = repo.list_contract_versions(name="todos-api")
    assert len(versions) == 2
    assert {v["version"] for v in versions} == {"1.0.0", "1.1.0"}


def test_consumer_pins_to_exact_version(tmp_path):
    service, repo = _service(tmp_path)
    publisher = _ensure_publisher_task(service)
    service.publish_contract(contract=_contract(task=publisher))
    consumer_id, _ = (
        service.plan(
            repository="owner/consumer", issue_number=2, base_branch="main",
            base_sha=BASE_SHA,
        ).task.task_id,
        None,
    )
    pin = service.pin_contract(
        contract_name="todos-api", version="1.0.0", consumer_task_id=consumer_id
    )
    assert pin.digest == service.publish_contract(
        contract=_contract(task=publisher)
    ).digest
    pins = service.contract_consumers(contract_name="todos-api")
    assert len(pins) == 1
    assert pins[0]["consumer_task_id"] == consumer_id


def test_consumer_pin_repin_updates_version(tmp_path):
    service, repo = _service(tmp_path)
    publisher = _ensure_publisher_task(service)
    service.publish_contract(contract=_contract(version="1.0.0", task=publisher))
    service.publish_contract(contract=_contract(version="1.1.0", task=publisher))
    consumer_id = service.plan(
        repository="owner/consumer", issue_number=2, base_branch="main",
        base_sha=BASE_SHA,
    ).task.task_id
    service.pin_contract(
        contract_name="todos-api", version="1.0.0", consumer_task_id=consumer_id
    )
    service.pin_contract(
        contract_name="todos-api", version="1.1.0", consumer_task_id=consumer_id
    )
    pins = service.contract_consumers(contract_name="todos-api")
    # The consumer's single pin now points at 1.1.0.
    assert [p["version"] for p in pins if p["consumer_task_id"] == consumer_id] == ["1.1.0"]


def test_pin_unknown_version_refused(tmp_path):
    service, _ = _service(tmp_path)
    _ensure_publisher_task(service)
    consumer_id = service.plan(
        repository="owner/consumer", issue_number=2, base_branch="main",
        base_sha=BASE_SHA,
    ).task.task_id
    with pytest.raises(NotFoundError, match="not found"):
        service.pin_contract(
            contract_name="todos-api", version="9.9.9",
            consumer_task_id=consumer_id,
        )
