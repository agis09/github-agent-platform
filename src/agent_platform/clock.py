"""Clock abstraction for deterministic time in tests."""

import datetime
from typing import Protocol


def utcnow() -> datetime.datetime:
    """Current UTC time with timezone (naive-UTC is not produced)."""
    return datetime.datetime.now(tz=datetime.UTC)


class Clock(Protocol):
    def now(self) -> datetime.datetime: ...


class SystemClock:
    def now(self) -> datetime.datetime:
        return utcnow()


class FixedClock:
    """Deterministic clock; tests advance it explicitly."""

    def __init__(self, start: datetime.datetime | None = None) -> None:
        self._now = start or datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

    def now(self) -> datetime.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += datetime.timedelta(seconds=seconds)
