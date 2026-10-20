"""Stand-in for the Visa network between Airwallex acquiring (merchant side) and Airwallex issuing
(buyer side).

In production the merchant's authorization travels acquirer -> Visa -> issuer. In the sandbox the two
sides are separate APIs, so this bridge carries each message: an authorization goes to the issuer
first (the buyer's leg card, its controls and the basket ledger), and only an approved one becomes a
hold on the merchant's payment intent. Captures and voids are mirrored the same way, so the card
ledger on the buyer's side shows exactly what the merchants did.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .airwallex.base import AirwallexError, CardDetails, Intent
from .clock import CLOCK
from .events import BUS, Bus

# Airwallex's documented always-succeeds sandbox test card, used on the acquiring side in live mode.
SANDBOX_TEST_CARD = CardDetails("4035501000000008", "12", "2030", "123", "Keystone Agent")


class Declined(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Link:
    intent_id: str
    card_id: str
    auth_txn_id: str
    amount: Decimal
    currency: str
    merchant: str
    authorized_at: datetime
    reversed_at: datetime | None = None
    captured: Decimal | None = None
    refused: list[dict] = field(default_factory=list)


class Network:
    def __init__(self, acquirer, issuer, bus: Bus = BUS, live: bool = False, ledger=None):
        self.acq = acquirer
        self.ledger = ledger
        self.iss = issuer
        self.bus = bus
        self.live = live
        self.routes: dict[str, str] = {}  # PAN -> card_id (BIN routing stand-in)
        self.links: dict[str, Link] = {}

    def register_card(self, pan: str, card_id: str) -> None:
        self.routes[pan] = card_id

    async def authorize_issuer(self, intent: Intent, card: CardDetails, mcc: int, merchant: str) -> Link:
        """The issuer half only: used when the acquirer half is completed in the buyer's browser."""
        card_id = self.routes.get(card.number, "")
        txn = await self.iss.simulate_authorization(card_id, card.number, intent.amount, intent.currency, mcc, merchant)
        self.bus.emit("issuer.authorization", "issuer", card_id=card_id or txn.card_id, txn=txn.id,
                      amount=str(intent.amount), currency=intent.currency, merchant=merchant, status=txn.status,
                      reason=txn.failure_reason)
        if txn.status == "FAILED":
            raise Declined(txn.failure_reason or "DECLINED")
        link = Link(intent.id, card_id or txn.card_id, txn.id, intent.amount, intent.currency, merchant, CLOCK.now())
        self.links[intent.id] = link
        return link

    async def authorize(self, intent: Intent, card: CardDetails, mcc: int, merchant: str, request_id: str,
                        pre_auth: bool) -> Link:
        # The simulation routes by card number, so an unregistered PAN still reaches the issuer.
        card_id = self.routes.get(card.number, "")
        txn = await self.iss.simulate_authorization(card_id, card.number, intent.amount, intent.currency, mcc, merchant)
        self.bus.emit("issuer.authorization", "issuer", card_id=card_id, txn=txn.id, amount=str(intent.amount),
                      currency=intent.currency, merchant=merchant, status=txn.status, reason=txn.failure_reason)
        if txn.status == "FAILED":
            raise Declined(txn.failure_reason or "DECLINED")
        acq_card = SANDBOX_TEST_CARD if self.live else card
        try:
            await self.acq.confirm_hold(intent.id, acq_card, request_id, pre_auth)
        except AirwallexError:
            await self.iss.simulate_reverse(txn.id, intent.amount)
            raise
        link = Link(intent.id, card_id, txn.id, intent.amount, intent.currency, merchant, CLOCK.now())
        self.links[intent.id] = link
        return link

    async def capture(self, intent_id: str, amount: Decimal, request_id: str) -> Intent:
        link = self.links[intent_id]
        try:
            it = await self.acq.capture(intent_id, amount, request_id)
        except AirwallexError as e:
            link.refused.append({"op": "capture", "code": e.code, "message": e.message, "at": CLOCK.now().isoformat()})
            self.bus.emit("network.capture_refused", "network", intent=intent_id, merchant=link.merchant,
                          code=e.code, message=e.message, reversed=link.reversed_at is not None)
            raise
        clr = await self.iss.simulate_capture(link.auth_txn_id, amount)
        link.captured = amount
        self.bus.emit("issuer.clearing", "issuer", card_id=link.card_id, txn=clr.id, amount=str(amount),
                      currency=link.currency, merchant=link.merchant, status=clr.status, reason=clr.failure_reason)
        return it

    async def void(self, intent_id: str, reason: str, request_id: str) -> Intent:
        link = self.links[intent_id]
        it = await self.acq.cancel(intent_id, reason, request_id)
        if link.reversed_at is None:
            rev = await self.iss.simulate_reverse(link.auth_txn_id, link.amount)
            link.reversed_at = CLOCK.now()
            if self.ledger is not None:
                self.ledger.released(link.card_id)
            self.bus.emit("issuer.reversal", "issuer", card_id=link.card_id, txn=rev.id, amount=str(link.amount),
                          currency=link.currency, merchant=link.merchant)
        return it

    async def refund(self, intent_id: str, amount: Decimal, request_id: str) -> dict:
        link = self.links[intent_id]
        r = await self.acq.refund(intent_id, amount, request_id)
        t = await self.iss.simulate_refund(link.auth_txn_id, amount)
        self.bus.emit("issuer.refund", "issuer", card_id=link.card_id, txn=t.id, amount=str(amount),
                      currency=link.currency, merchant=link.merchant)
        return r
