"""Event bus: every money movement and decision is published here, for the console and the evidence log."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Event:
    kind: str
    actor: str
    data: dict
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    seq: int = 0

    def to_json(self) -> dict:
        return {"seq": self.seq, "at": self.at, "kind": self.kind, "actor": self.actor, "data": self.data}


class Bus:
    def __init__(self) -> None:
        self.history: list[Event] = []
        self._subs: list[asyncio.Queue] = []
        self._sinks: list[Any] = []

    def emit(self, kind: str, actor: str, **data: Any) -> Event:
        ev = Event(kind, actor, _plain(data), seq=len(self.history) + 1)
        self.history.append(ev)
        for q in list(self._subs):
            q.put_nowait(ev)
        for s in self._sinks:
            s(ev)
        return ev

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def add_sink(self, fn) -> None:
        self._sinks.append(fn)

    def load(self, path: str, keep: int = 2000) -> None:
        """Reload the event history a previous process wrote, so the console keeps its timeline."""
        import os
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()][-keep:]
        for r in rows:
            self.history.append(Event(r["kind"], r["actor"], r["data"], r["at"], seq=len(self.history) + 1))

    def jsonl_sink(self, path: str):
        def write(ev: Event) -> None:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev.to_json()) + "\n")
        self.add_sink(write)


def _plain(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if isinstance(o, (str, int, bool)) or o is None:
        return o
    if isinstance(o, float):
        return o
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


BUS = Bus()
