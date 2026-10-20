"""The basket mandate: one instruction, approved once with a passkey, that covers every leg.

The person approves the legs, each leg's constraints and substitution rules, a total cap in home
currency and the all-or-nothing flag. The approval signs the mandate's canonical digest, so any
change to any term produces a different digest and needs a new approval.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from .canonical import digest

LegKind = Literal["flight", "hotel", "event", "restaurant", "component", "transport"]
Op = Literal["eq", "ne", "le", "ge", "lt", "gt", "in", "contains"]


class Cond(BaseModel):
    attr: str
    op: Op
    value: Any

    def holds(self, attrs: dict) -> bool:
        if self.attr not in attrs:
            return False
        a, v = attrs[self.attr], self.value
        if self.op in ("le", "ge", "lt", "gt"):
            a, v = Decimal(str(a)), Decimal(str(v))
        return {
            "eq": lambda: a == v, "ne": lambda: a != v, "le": lambda: a <= v, "ge": lambda: a >= v,
            "lt": lambda: a < v, "gt": lambda: a > v, "in": lambda: a in v, "contains": lambda: v in a,
        }[self.op]()

    def describe(self) -> str:
        sym = {"eq": "=", "ne": "!=", "le": "<=", "ge": ">=", "lt": "<", "gt": ">", "in": "in", "contains": "has"}
        return f"{self.attr} {sym[self.op]} {self.value}"


class Substitution(BaseModel):
    allowed: bool = False
    require: list[Cond] = Field(default_factory=list)
    max_price: Decimal | None = None  # leg currency; None means the leg's max_price


class LegSpec(BaseModel):
    leg_id: str
    kind: LegKind
    title: str
    currency: str
    mcc: int
    quantity: int = 1
    max_price: Decimal  # total for the leg, leg currency
    require: list[Cond] = Field(default_factory=list)
    preferred_merchant: str | None = None
    substitution: Substitution = Field(default_factory=Substitution)


class Principal(BaseModel):
    name: str
    organisation: str
    email: str | None = None


class Mandate(BaseModel):
    schema_: str = Field("keystone.basket/1", alias="schema")
    mandate_id: str
    principal: Principal
    purpose: str
    home_currency: str = "USD"
    cap: Decimal
    atomic: bool = True
    commit_by: datetime
    legs: list[LegSpec]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0))

    model_config = {"populate_by_name": True}

    def canonical_json(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)

    def digest(self) -> str:
        return digest(self.canonical_json())

    def leg(self, leg_id: str) -> LegSpec:
        for l in self.legs:
            if l.leg_id == leg_id:
                return l
        raise KeyError(leg_id)


class Offer(BaseModel):
    merchant_id: str
    offer_id: str
    leg_kind: LegKind
    title: str
    unit_price: Decimal
    currency: str
    mcc: int
    available: int
    attrs: dict = Field(default_factory=dict)

    def total(self, quantity: int) -> Decimal:
        return self.unit_price * quantity


class Fit(BaseModel):
    ok: bool
    substitute: bool = False
    reasons: list[str] = Field(default_factory=list)


def fit(leg: LegSpec, offer: Offer) -> Fit:
    """Does this offer satisfy the leg as written, as an allowed substitute, or not at all?"""
    reasons: list[str] = []
    if offer.currency != leg.currency:
        return Fit(ok=False, reasons=[f"currency {offer.currency} != {leg.currency}"])
    if offer.mcc != leg.mcc:
        return Fit(ok=False, reasons=[f"merchant category {offer.mcc} != {leg.mcc}"])
    if offer.available < leg.quantity:
        return Fit(ok=False, reasons=[f"only {offer.available} available, need {leg.quantity}"])
    total = offer.total(leg.quantity)
    primary_fail = [c.describe() for c in leg.require if not c.holds(offer.attrs)]
    if total <= leg.max_price and not primary_fail and (
            leg.preferred_merchant is None or leg.preferred_merchant == offer.merchant_id):
        return Fit(ok=True)
    if not leg.substitution.allowed:
        reasons += [f"fails {r}" for r in primary_fail]
        if total > leg.max_price:
            reasons.append(f"price {total} > {leg.max_price}")
        if leg.preferred_merchant and leg.preferred_merchant != offer.merchant_id:
            reasons.append(f"merchant {offer.merchant_id} is not the named one")
        return Fit(ok=False, reasons=reasons or ["no substitution allowed"])
    cap = leg.substitution.max_price if leg.substitution.max_price is not None else leg.max_price
    sub_fail = [c.describe() for c in leg.substitution.require if not c.holds(offer.attrs)]
    if sub_fail or total > cap:
        reasons += [f"fails {r}" for r in sub_fail]
        if total > cap:
            reasons.append(f"price {total} > substitute cap {cap}")
        return Fit(ok=False, reasons=reasons)
    return Fit(ok=True, substitute=True)
