"""Buyer side: one Airwallex Visa virtual card per leg, and the basket ledger that decides every
authorization on those cards.

The ledger has the shape of an Airwallex remote-authorization endpoint (``decide`` takes the
authorization request and answers AUTHORIZED or DECLINED with a reason). It declines a hold that
would push the basket over its cap, a second hold on a leg, and any hold on a basket that is closed.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from .airwallex.base import Card, CardDetails, Controls
from .clock import CLOCK
from .events import BUS, Bus
from .mandate import LegSpec, Mandate
from .money import quantize


@dataclass
class LegBook:
    leg_id: str
    card_id: str
    currency: str
    rate_to_home: Decimal
    held: Decimal = Decimal(0)
    txn: str | None = None


@dataclass
class BasketBook:
    basket_id: str
    home_currency: str
    cap: Decimal
    status: str = "OPEN"  # OPEN | COMMITTED | ABORTED
    legs: dict[str, LegBook] = field(default_factory=dict)

    def held_home(self) -> Decimal:
        return sum((l.held * l.rate_to_home for l in self.legs.values()), Decimal(0))


class BasketLedger:
    def __init__(self, bus: Bus = BUS):
        self.bus = bus
        self.baskets: dict[str, BasketBook] = {}
        self.cards: dict[str, tuple[str, str]] = {}
        self.pans: dict[str, str] = {}  # sha256(PAN) -> card_id, for relayed requests that carry no card_id
        self.decisions: list[dict] = []

    def open(self, basket_id: str, home_currency: str, cap: Decimal) -> BasketBook:
        b = self.baskets.setdefault(basket_id, BasketBook(basket_id, home_currency, cap))
        return b

    def bind(self, basket_id: str, leg_id: str, card_id: str, currency: str, rate_to_home: Decimal) -> None:
        b = self.baskets[basket_id]
        old = b.legs.get(leg_id)
        if old is not None:
            self.cards.pop(old.card_id, None)
        b.legs[leg_id] = LegBook(leg_id, card_id, currency, rate_to_home)
        self.cards[card_id] = (basket_id, leg_id)

    def bind_pan(self, card_id: str, pan: str) -> None:
        self.pans[hashlib.sha256(pan.encode()).hexdigest()] = card_id

    def set_rate(self, basket_id: str, leg_id: str, rate_to_home: Decimal) -> None:
        self.baskets[basket_id].legs[leg_id].rate_to_home = rate_to_home

    def close(self, basket_id: str, status: str) -> None:
        self.baskets[basket_id].status = status

    async def decide(self, req: dict) -> dict:
        card_id = req.get("card_id") or self.pans.get(req.get("card_number_sha256", ""), "")
        if card_id not in self.cards and req.get("card_number_sha256") in self.pans:
            card_id = self.pans[req["card_number_sha256"]]
        amount = Decimal(req["transaction_amount"])
        decision = self._decide(card_id, amount, req)
        self.decisions.append({"request": req, "decision": decision, "at": CLOCK.now().isoformat()})
        self.bus.emit("ledger.decision", "issuer", amount=str(amount), currency=req.get("transaction_currency"),
                      **{"card_id": card_id, **decision})
        return decision

    def _decide(self, card_id: str, amount: Decimal, req: dict) -> dict:
        where = self.cards.get(card_id)
        if where is None:
            return {"response_status": "DECLINED", "status_reason": "CARD_NOT_IN_A_BASKET"}
        b = self.baskets[where[0]]
        leg = b.legs[where[1]]
        if b.status != "OPEN":
            return {"response_status": "DECLINED", "status_reason": f"BASKET_{b.status}"}
        if leg.held > 0:
            return {"response_status": "DECLINED", "status_reason": "LEG_ALREADY_HELD"}
        after = b.held_home() + amount * leg.rate_to_home
        if after > b.cap:
            return {"response_status": "DECLINED", "status_reason": "BASKET_CAP_EXCEEDED",
                    "detail": f"{quantize(after, b.home_currency)} > {b.cap} {b.home_currency}"}
        leg.held = amount
        leg.txn = req.get("card_transaction_id")
        return {"response_status": "AUTHORIZED", "basket_id": b.basket_id, "leg_id": leg.leg_id, "card_id": card_id,
                "basket_held": str(quantize(after, b.home_currency))}

    def released(self, card_id: str) -> None:
        where = self.cards.get(card_id)
        if where:
            self.baskets[where[0]].legs[where[1]].held = Decimal(0)


class LegCards:
    """Issues and retires the per-leg cards."""

    def __init__(self, issuing, network, ledger: BasketLedger, bus: Bus = BUS):
        self.iss = issuing
        self.net = network
        self.ledger = ledger
        self.bus = bus
        self.by_leg: dict[tuple[str, str], tuple[Card, CardDetails]] = {}
        self.on_issue = None  # callback(basket_id, leg_id, card_id, last4), used to journal every card

    async def issue(self, basket_id: str, mandate: Mandate, leg: LegSpec, rate_to_home: Decimal,
                    limit: Decimal | None = None) -> tuple[Card, CardDetails]:
        ceiling = limit if limit is not None else max(
            leg.max_price, leg.substitution.max_price or Decimal(0)) if leg.substitution.allowed else leg.max_price
        now = CLOCK.now()
        controls = Controls(per_transaction_limit=quantize(ceiling, leg.currency), currency=leg.currency,
                            allowed_mccs=[leg.mcc], active_from=now - timedelta(minutes=1),
                            active_to=mandate.commit_by, all_time_limit=quantize(ceiling, leg.currency))
        holder = await self.iss.ensure_cardholder()
        old = self.by_leg.get((basket_id, leg.leg_id))
        if old:
            await self.iss.set_card_status(old[0].id, "CLOSED")
        card = await self.iss.create_card(holder, controls, f"{basket_id[:8]}-{leg.leg_id}",
                                          str(uuid.uuid4()))
        details = await self.iss.card_details(card.id)
        self.net.register_card(details.number, card.id)
        self.ledger.bind_pan(card.id, details.number)
        self.ledger.bind(basket_id, leg.leg_id, card.id, leg.currency, rate_to_home)
        self.by_leg[(basket_id, leg.leg_id)] = (card, details)
        if self.on_issue:
            self.on_issue(basket_id, leg.leg_id, card.id, details.number[-4:])
        self.bus.emit("issuer.card_issued", "issuer", basket_id=basket_id, leg_id=leg.leg_id, card_id=card.id,
                      last4=details.number[-4:], limit=str(controls.per_transaction_limit), currency=leg.currency,
                      mcc=leg.mcc, active_to=mandate.commit_by.isoformat())
        return card, details

    async def close_all(self, basket_id: str) -> None:
        for (b, leg_id), (card, _) in list(self.by_leg.items()):
            if b == basket_id and card.status != "CLOSED":
                await self.iss.set_card_status(card.id, "CLOSED")
                card.status = "CLOSED"
                self.bus.emit("issuer.card_closed", "issuer", basket_id=basket_id, leg_id=leg_id, card_id=card.id)
