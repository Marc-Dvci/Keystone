from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    """Wall clock with an offset, so tests and the replay can move time forward (hold expiry)."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset

    def advance(self, delta: timedelta) -> None:
        self.offset += delta


CLOCK = Clock()
