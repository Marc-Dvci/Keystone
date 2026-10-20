"""Durable coordinator log (SQLite, WAL). Every phase is written before it is sent, and the
commit-or-abort decision is written before any participant hears it, so a coordinator that dies
can be restarted and finish the basket from the log alone."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
create table if not exists entries (
  id integer primary key autoincrement,
  basket_id text not null,
  kind text not null,
  leg_id text,
  data text not null,
  at text not null
);
create index if not exists entries_basket on entries(basket_id, id);
create table if not exists baskets (
  basket_id text primary key,
  mandate text not null,
  approval text not null,
  status text not null,
  created_at text not null
);
"""


class Journal:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=full")
        self.db.executescript(SCHEMA)

    def open_basket(self, basket_id: str, mandate: dict, approval: dict) -> None:
        with self._lock:
            self.db.execute("insert or ignore into baskets values (?,?,?,?,?)",
                            (basket_id, json.dumps(mandate), json.dumps(approval), "OPEN", _now()))

    def set_status(self, basket_id: str, status: str) -> None:
        with self._lock:
            self.db.execute("update baskets set status=? where basket_id=?", (status, basket_id))

    def append(self, basket_id: str, kind: str, leg_id: str | None = None, **data: Any) -> None:
        with self._lock:
            self.db.execute("insert into entries(basket_id,kind,leg_id,data,at) values (?,?,?,?,?)",
                            (basket_id, kind, leg_id, json.dumps(data, default=str), _now()))

    def entries(self, basket_id: str) -> list[dict]:
        with self._lock:
            rows = self.db.execute("select kind, leg_id, data, at from entries where basket_id=? order by id",
                                   (basket_id,)).fetchall()
        return [{"kind": k, "leg_id": l, "data": json.loads(d), "at": a} for k, l, d, a in rows]

    def unfinished(self) -> list[tuple[str, dict, dict]]:
        with self._lock:
            rows = self.db.execute("select basket_id, mandate, approval from baskets where status not in "
                                   "('COMMITTED','ABORTED')").fetchall()
        return [(b, json.loads(m), json.loads(a)) for b, m, a in rows]

    def basket(self, basket_id: str) -> dict | None:
        with self._lock:
            r = self.db.execute("select mandate, approval, status from baskets where basket_id=?",
                                (basket_id,)).fetchone()
        return None if r is None else {"mandate": json.loads(r[0]), "approval": json.loads(r[1]), "status": r[2]}

    def decision(self, basket_id: str) -> str | None:
        for e in self.entries(basket_id):
            if e["kind"] in ("decision.commit", "decision.abort"):
                return e["kind"].split(".")[1]
        return None

    def close(self) -> None:
        self.db.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
