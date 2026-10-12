"""Shared test fixtures: tmp home, FixedClock, service assembly."""

import datetime
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_platform.adapters.agent.fake import FakeAgentAdapter
from agent_platform.adapters.artifacts.local import LocalFsArtifactStore
from agent_platform.clock import FixedClock
from agent_platform.config import Settings
from agent_platform.orchestrator.repository import SqliteTaskRepository
from agent_platform.orchestrator.service import TaskService

UTC = datetime.UTC


class RecordingAdapter(FakeAgentAdapter):
    """Fake adapter that counts calls — for idempotency assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.plan_calls = 0
        self.execute_calls = 0
        self.review_calls = 0

    def plan(self, job):
        self.plan_calls += 1
        return super().plan(job)

    def execute(self, job):
        self.execute_calls += 1
        return super().execute(job)

    def review(self, job):
        self.review_calls += 1
        return super().review(job)


TEST_APP_KEY = Path(__file__).resolve().parent / "fixtures" / "test-app-key.pem"


@pytest.fixture(scope="session", autouse=True)
def _test_app_key() -> Path:
    """Throwaway RSA key for GitHub App config tests, generated once per
    session (gitignored; a real key never appears in tests)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if not TEST_APP_KEY.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        TEST_APP_KEY.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        TEST_APP_KEY.chmod(0o600)
    return TEST_APP_KEY


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime.datetime(2026, 9, 15, 9, 0, 0, tzinfo=UTC))


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def settings(home: Path) -> Settings:
    return Settings(
        home_dir=str(home),
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


@pytest.fixture
def repository(home: Path) -> Iterator[SqliteTaskRepository]:
    repo = SqliteTaskRepository(home / "state.sqlite3")
    yield repo
    repo.close()


@pytest.fixture
def artifacts(home: Path, settings: Settings) -> LocalFsArtifactStore:
    return LocalFsArtifactStore(home / "artifacts", settings.artifact_max_size)


@pytest.fixture
def service(
    repository: SqliteTaskRepository,
    artifacts: LocalFsArtifactStore,
    clock: FixedClock,
    settings: Settings,
) -> TaskService:
    return TaskService(
        repository=repository,
        adapter=FakeAgentAdapter(),
        artifacts=artifacts,
        clock=clock,
        settings=settings,
    )


@pytest.fixture
def recording_service(
    repository: SqliteTaskRepository,
    artifacts: LocalFsArtifactStore,
    clock: FixedClock,
    settings: Settings,
) -> tuple[TaskService, RecordingAdapter]:
    adapter = RecordingAdapter()
    service = TaskService(
        repository=repository,
        adapter=adapter,
        artifacts=artifacts,
        clock=clock,
        settings=settings,
    )
    return service, adapter


BASE_SHA = "a" * 40
