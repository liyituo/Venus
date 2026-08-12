from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

UTC = UTC


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._value = value.astimezone(UTC)

    def now(self) -> datetime:
        return self._value

    def advance(self, **kwargs: int) -> None:
        self._value += timedelta(**kwargs)
