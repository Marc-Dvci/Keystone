"""Visa rules that bound a hold-then-capture basket, checked in code.

Source: Visa Core Rules and Visa Product and Service Rules, public edition 18 Apr 2026.
- Table 5-12 (s.5.7.3.5): how long an authorization stays valid.
- Table 5-14 (s.5.7.3.6): an authorization that will not be completed is reversed within 24 hours.
- s.1.7.7.2: a transaction reversed for its full amount must not be deposited.
- s.5.7.3.1: no split transactions (one sale, one authorization).
Airwallex adds: one capture per payment intent, never above the authorized amount.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

REVERSAL_DEADLINE = timedelta(hours=24)

AIRLINE = set(range(3000, 3351)) | {4511}
CAR_RENTAL = set(range(3351, 3501)) | {7512}
LODGING = set(range(3501, 4000)) | {7011}
CRUISE = {4411}
ESTIMATED_AUTH_MCCS = LODGING | CAR_RENTAL | CRUISE


@dataclass(frozen=True)
class Check:
    rule: str
    ok: bool
    detail: str

    def to_json(self) -> dict:
        return {"rule": self.rule, "ok": self.ok, "detail": self.detail}


def validity(mcc: int, *, extended: bool = False, merchant_initiated: bool = False) -> timedelta:
    """Table 5-12, card-absent environment."""
    if merchant_initiated:
        return timedelta(days=5)
    if mcc in ESTIMATED_AUTH_MCCS or extended:
        return timedelta(days=30)
    return timedelta(days=10)


def check_hold_window(mcc: int, authorized_at: datetime, commit_by: datetime, *, extended: bool = False) -> Check:
    end = authorized_at + validity(mcc, extended=extended)
    ok = commit_by <= end
    return Check("visa.5-12.validity", ok,
                 f"hold valid to {end.isoformat()}, basket commits by {commit_by.isoformat()}")


def check_capture(authorized: Decimal, capture: Decimal, already_captured: bool, reversed_in_full: bool,
                  authorized_at: datetime, now: datetime, mcc: int, *, extended: bool = False) -> list[Check]:
    out = [
        Check("visa.1.7.7.2.no_deposit_after_reversal", not reversed_in_full,
              "authorization was reversed in full" if reversed_in_full else "authorization live"),
        Check("airwallex.capture_le_authorized", capture <= authorized, f"capture {capture} vs hold {authorized}"),
        Check("airwallex.single_capture", not already_captured, "already captured" if already_captured else "first capture"),
    ]
    end = authorized_at + validity(mcc, extended=extended)
    out.append(Check("visa.5-12.validity", now <= end, f"capture at {now.isoformat()}, hold valid to {end.isoformat()}"))
    return out


def check_reversal_latency(cancelled_at: datetime, reversed_at: datetime | None, now: datetime) -> Check:
    deadline = cancelled_at + REVERSAL_DEADLINE
    if reversed_at is None:
        return Check("visa.5-14.reverse_24h", now <= deadline, f"not yet reversed; deadline {deadline.isoformat()}")
    lat = (reversed_at - cancelled_at).total_seconds()
    return Check("visa.5-14.reverse_24h", reversed_at <= deadline, f"reversed {lat:.2f}s after abort")


def check_no_split(merchant_leg_pairs: list[tuple[str, str]]) -> Check:
    seen: set[tuple[str, str]] = set()
    dup = [p for p in merchant_leg_pairs if p in seen or seen.add(p)]  # type: ignore[func-returns-value]
    return Check("visa.5.7.3.1.no_split", not dup,
                 "one authorization per leg per merchant" if not dup else f"duplicate holds {dup}")
