"""In-memory Airwallex with the documented sandbox behaviour Keystone relies on.

- Amounts in major units; request_id reuse with a different payload is ``duplicate_request_id``,
  a retry of the same call returns the first result.
- A payment intent takes one capture, never above the authorized amount; cancel moves it to
  CANCELLED; a hold lapses after 7 days (30 with pre_auth).
- Issued cards enforce their authorization controls with the sandbox decline reasons
  (LIMIT_EXCEEDED, CURRENCY_NOT_ALLOWED, MERCHANT_CATEGORY_NOT_ALLOWED, OUT_OF_ALLOWED_TIME_RANGE,
  CARD_INACTIVE, CARD_CLOSED, INSUFFICIENT_FUNDS) and call a remote-authorization hook if one is set.
- FX quotes lock ``client_rate`` until ``valid_to``; conversions settle at once.
"""
from __future__ import annotations

import itertools
import math
import uuid
from datetime import timedelta
from decimal import Decimal

from ..clock import CLOCK, Clock
from ..money import quantize
from .base import (CANCELLED, REQUIRES_CAPTURE, REQUIRES_PAYMENT_METHOD, SUCCEEDED, AirwallexError, Card,
                   CardDetails, CardTxn, Controls, Conversion, Intent, Quote, RemoteAuth)

VALIDITY = {"MIN_1": timedelta(minutes=1), "MIN_15": timedelta(minutes=15), "MIN_30": timedelta(minutes=30),
            "HR_1": timedelta(hours=1), "HR_4": timedelta(hours=4), "HR_8": timedelta(hours=8),
            "HR_24": timedelta(hours=24)}

BASE_RATES = {("USD", "EUR"): Decimal("0.9180"), ("USD", "CNY"): Decimal("7.1150"), ("USD", "GBP"): Decimal("0.7840")}
DECLINE_CARDS = {"4000000000000002": "DECLINED"}


class FakeAirwallex:
    mode = "replay"

    def __init__(self, clock: Clock = CLOCK, balances: dict[str, Decimal] | None = None):
        self.clock = clock
        self.intents: dict[str, Intent] = {}
        self.intent_meta: dict[str, dict] = {}
        self.cards: dict[str, Card] = {}
        self.card_numbers: dict[str, str] = {}
        self.txns: dict[str, CardTxn] = {}
        self.quotes: dict[str, Quote] = {}
        self.conversions: list[Conversion] = []
        self.balances_: dict[str, Decimal] = balances or {"USD": Decimal("250000"), "EUR": Decimal("0"),
                                                          "CNY": Decimal("0"), "GBP": Decimal("0")}
        self.remote_auth: RemoteAuth | None = None
        self.calls: list[tuple[str, str]] = []
        self._req: dict[str, tuple[str, object]] = {}
        self._seq = itertools.count(1)
        self.cardholder_id: str | None = None
        self.spot_drift = Decimal("0.004")  # amplitude of the deterministic spot wobble

    # ---------- helpers
    def _id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._seq):05d}{uuid.uuid4().hex[:8]}"

    def _idem(self, request_id: str, fingerprint: str):
        hit = self._req.get(request_id)
        if hit is None:
            return None
        if hit[0] != fingerprint:
            raise AirwallexError("duplicate_request_id", "request_id reused for a different request")
        return hit[1]

    def _remember(self, request_id: str, fingerprint: str, result):
        self._req[request_id] = (fingerprint, result)
        return result

    def _expire(self, it: Intent) -> None:
        meta = self.intent_meta.get(it.id, {})
        at = meta.get("authorized_at")
        if it.status == REQUIRES_CAPTURE and at is not None:
            life = timedelta(days=30 if meta.get("pre_auth") else 7)
            if self.clock.now() > at + life:
                it.status = CANCELLED
                meta["cancel_reason"] = "authorization_expired"

    # ---------- acquiring
    async def create_intent(self, amount, currency, merchant_order_id, request_id, metadata):
        self.calls.append(("POST", "/pa/payment_intents/create"))
        fp = f"create:{amount}:{currency}:{merchant_order_id}"
        if (r := self._idem(request_id, fp)) is not None:
            return r
        it = Intent(self._id("int"), quantize(amount, currency), currency, REQUIRES_PAYMENT_METHOD,
                    merchant_order_id, metadata=dict(metadata))
        self.intents[it.id] = it
        self.intent_meta[it.id] = {}
        return self._remember(request_id, fp, it)

    async def confirm_hold(self, intent_id, card: CardDetails, request_id, pre_auth):
        self.calls.append(("POST", f"/pa/payment_intents/{intent_id}/confirm"))
        fp = f"confirm:{intent_id}:{card.number[-4:]}:{pre_auth}"
        if (r := self._idem(request_id, fp)) is not None:
            return r
        it = self._get(intent_id)
        if it.status != REQUIRES_PAYMENT_METHOD:
            raise AirwallexError("invalid_status_for_operation", f"intent is {it.status}")
        if card.number in DECLINE_CARDS:
            raise AirwallexError("payment_method_declined", "issuer declined", 400)
        self.intent_meta[it.id].update(authorized_at=self.clock.now(), pre_auth=pre_auth, card_last4=card.number[-4:])
        it.status = REQUIRES_CAPTURE
        return self._remember(request_id, fp, it)

    async def capture(self, intent_id, amount, request_id):
        self.calls.append(("POST", f"/pa/payment_intents/{intent_id}/capture"))
        fp = f"capture:{intent_id}:{amount}"
        if (r := self._idem(request_id, fp)) is not None:
            return r
        it = self._get(intent_id)
        self._expire(it)
        if it.status == CANCELLED:
            raise AirwallexError("invalid_status_for_operation", "cannot capture a cancelled payment intent")
        if it.status == SUCCEEDED:
            raise AirwallexError("invalid_status_for_operation", "payment intent already captured")
        if it.status != REQUIRES_CAPTURE:
            raise AirwallexError("invalid_status_for_operation", f"intent is {it.status}")
        amount = quantize(amount, it.currency)
        if amount > it.amount:
            raise AirwallexError("validation_error", "capture amount exceeds authorized amount")
        it.status = SUCCEEDED
        it.captured_amount = amount
        return self._remember(request_id, fp, it)

    async def cancel(self, intent_id, reason, request_id):
        self.calls.append(("POST", f"/pa/payment_intents/{intent_id}/cancel"))
        fp = f"cancel:{intent_id}"
        if (r := self._idem(request_id, fp)) is not None:
            return r
        it = self._get(intent_id)
        self._expire(it)
        if it.status == SUCCEEDED:
            raise AirwallexError("invalid_status_for_operation", "cannot cancel a captured payment intent")
        if it.status != CANCELLED:
            it.status = CANCELLED
            self.intent_meta[it.id]["cancel_reason"] = reason
            self.intent_meta[it.id]["cancelled_at"] = self.clock.now()
        return self._remember(request_id, fp, it)

    async def refund(self, intent_id, amount, request_id, reason="keystone_compensation"):
        self.calls.append(("POST", "/pa/refunds/create"))
        fp = f"refund:{intent_id}:{amount}"
        if (r := self._idem(request_id, fp)) is not None:
            return r
        it = self._get(intent_id)
        if it.status != SUCCEEDED:
            raise AirwallexError("invalid_status_for_operation", "refund needs a SUCCEEDED intent")
        meta = self.intent_meta[it.id]
        done = meta.get("refunded", Decimal(0))
        amount = quantize(amount, it.currency)
        if done + amount > it.captured_amount:
            raise AirwallexError("validation_error", "refund exceeds captured amount")
        meta["refunded"] = done + amount
        res = {"id": self._id("rfd"), "payment_intent_id": intent_id, "amount": amount, "status": "RECEIVED"}
        return self._remember(request_id, fp, res)

    async def get_intent(self, intent_id):
        it = self._get(intent_id)
        self._expire(it)
        return it

    def _get(self, intent_id) -> Intent:
        if intent_id not in self.intents:
            raise AirwallexError("resource_not_found", intent_id, 404)
        return self.intents[intent_id]

    # ---------- issuing
    async def ensure_cardholder(self):
        if self.cardholder_id is None:
            self.cardholder_id = self._id("ich")
        return self.cardholder_id

    async def create_card(self, cardholder_id, controls: Controls, nickname, request_id):
        self.calls.append(("POST", "/issuing/cards/create"))
        fp = f"card:{cardholder_id}:{nickname}"
        if (r := self._idem(request_id, fp)) is not None:
            return r
        card = Card(self._id("card"), "ACTIVE", controls, nickname)
        n = next(self._seq)
        self.card_numbers[card.id] = f"4242{n:012d}"[:16]
        self.cards[card.id] = card
        return self._remember(request_id, fp, card)

    async def card_details(self, card_id):
        return CardDetails(self.card_numbers[card_id], "12", str(self.clock.now().year + 3), "123")

    async def set_card_status(self, card_id, status):
        self.calls.append(("POST", f"/issuing/cards/{card_id}/update"))
        self.cards[card_id].status = status
        return self.cards[card_id]

    def _held_on(self, card_id: str) -> Decimal:
        total = Decimal(0)
        for t in self.txns.values():
            if t.card_id == card_id and t.status != "FAILED":
                if t.transaction_type in ("AUTHORIZATION",):
                    total += t.amount - t.raw.get("reversed", Decimal(0))
        return total

    async def simulate_authorization(self, card_id, card_number, amount, currency, mcc, merchant_name):
        self.calls.append(("POST", "/simulation/issuing/create"))
        card = self.cards[card_id]
        c = card.controls
        amount = quantize(amount, currency)
        now = self.clock.now()
        reason = None
        if card.status == "CLOSED":
            reason = "CARD_CLOSED"
        elif card.status != "ACTIVE":
            reason = "CARD_INACTIVE"
        elif (c.active_from and now < c.active_from) or (c.active_to and now > c.active_to):
            reason = "OUT_OF_ALLOWED_TIME_RANGE"
        elif currency != c.currency:
            reason = "CURRENCY_NOT_ALLOWED"
        elif c.allowed_mccs and mcc not in c.allowed_mccs:
            reason = "MERCHANT_CATEGORY_NOT_ALLOWED"
        elif amount > c.per_transaction_limit:
            reason = "LIMIT_EXCEEDED"
        elif c.all_time_limit is not None and self._held_on(card_id) + amount > c.all_time_limit:
            reason = "LIMIT_EXCEEDED"
        txn = CardTxn(self._id("txn"), card_id, "AUTHORIZATION", "PENDING", amount, currency, mcc, merchant_name)
        if reason is None and self.remote_auth is not None:
            decision = await self.remote_auth({
                "card_id": card_id, "card_transaction_id": txn.id, "transaction_type": "AUTHORIZATION",
                "transaction_amount": str(amount), "transaction_currency": currency,
                "merchant": {"name": merchant_name, "category_code": str(mcc)}})
            if decision.get("response_status") != "AUTHORIZED":
                reason = decision.get("status_reason") or "REMOTE_AUTH_DECLINED"
        if reason:
            txn.status, txn.failure_reason = "FAILED", reason
        self.txns[txn.id] = txn
        return txn

    async def simulate_capture(self, txn_id, amount):
        self.calls.append(("POST", f"/simulation/issuing/{txn_id}/capture"))
        auth = self.txns[txn_id]
        if auth.status == "FAILED":
            raise AirwallexError("validation_error", "cannot capture a failed authorization")
        if auth.raw.get("reversed", Decimal(0)) >= auth.amount:
            raise AirwallexError("validation_error", "authorization was reversed")
        amount = quantize(amount, auth.currency)
        bal = self.balances_.get(auth.currency, Decimal(0))
        if bal < amount:
            clr = CardTxn(self._id("txn"), auth.card_id, "CLEARING", "FAILED", amount, auth.currency, auth.mcc,
                          auth.merchant_name, "INSUFFICIENT_FUNDS")
            self.txns[clr.id] = clr
            return clr
        self.balances_[auth.currency] = bal - amount
        auth.status = "APPROVED"
        auth.raw["captured"] = amount
        clr = CardTxn(self._id("txn"), auth.card_id, "CLEARING", "APPROVED", amount, auth.currency, auth.mcc,
                      auth.merchant_name, raw={"authorization": txn_id})
        self.txns[clr.id] = clr
        return clr

    async def simulate_reverse(self, txn_id, amount):
        self.calls.append(("POST", f"/simulation/issuing/{txn_id}/reverse"))
        auth = self.txns[txn_id]
        amount = quantize(amount, auth.currency)
        auth.raw["reversed"] = auth.raw.get("reversed", Decimal(0)) + amount
        if auth.raw["reversed"] >= auth.amount:
            auth.status = "REVERSED"
        rev = CardTxn(self._id("txn"), auth.card_id, "REVERSAL", "APPROVED", amount, auth.currency, auth.mcc,
                      auth.merchant_name, raw={"authorization": txn_id})
        self.txns[rev.id] = rev
        return rev

    async def simulate_refund(self, txn_id, amount):
        self.calls.append(("POST", "/simulation/issuing/refund"))
        auth = self.txns[txn_id]
        amount = quantize(amount, auth.currency)
        self.balances_[auth.currency] = self.balances_.get(auth.currency, Decimal(0)) + amount
        t = CardTxn(self._id("txn"), auth.card_id, "REFUND", "APPROVED", amount, auth.currency, auth.mcc,
                    auth.merchant_name, raw={"authorization": txn_id})
        self.txns[t.id] = t
        return t

    async def card_transactions(self, card_id):
        return [t for t in self.txns.values() if t.card_id == card_id]

    # ---------- FX
    def _spot(self, sell: str, buy: str) -> Decimal:
        if sell == buy:
            return Decimal(1)
        if (sell, buy) in BASE_RATES:
            base = BASE_RATES[(sell, buy)]
        elif (buy, sell) in BASE_RATES:
            base = 1 / BASE_RATES[(buy, sell)]
        else:
            raise AirwallexError("unsupported_currency_pair", f"{sell}/{buy}")
        t = self.clock.now().timestamp()
        wobble = Decimal(str(math.sin(t / 3600.0))) * self.spot_drift
        return (base * (1 + wobble)).quantize(Decimal("0.000001"))

    async def spot(self, sell_currency, buy_currency):
        return self._spot(sell_currency, buy_currency)

    async def quote(self, sell_currency, buy_currency, buy_amount, validity, sell_amount=None):
        self.calls.append(("POST", "/fx/quotes/create"))
        rate = self._spot(sell_currency, buy_currency) * Decimal("0.9975")  # client rate carries a spread
        rate = rate.quantize(Decimal("0.000001"))
        if sell_amount is not None:
            sell = quantize(sell_amount, sell_currency)
            buy = quantize(sell * rate, buy_currency)
        else:
            buy = quantize(buy_amount, buy_currency)
            sell = quantize(Decimal(buy_amount) / rate, sell_currency)
        q = Quote(self._id("quote"), sell_currency, buy_currency, buy, sell, rate,
                  self.clock.now() + VALIDITY[validity])
        self.quotes[q.quote_id] = q
        return q

    async def convert(self, quote, request_id):
        self.calls.append(("POST", "/fx/conversions/create"))
        fp = f"conv:{quote.quote_id}"
        if (r := self._idem(request_id, fp)) is not None:
            return r
        q = self.quotes.get(quote.quote_id)
        if q is None:
            raise AirwallexError("invalid_quote_id", quote.quote_id)
        if self.clock.now() > q.valid_to:
            raise AirwallexError("expired_quote", quote.quote_id)
        if self.balances_.get(q.sell_currency, Decimal(0)) < q.sell_amount:
            raise AirwallexError("insufficient_funds", q.sell_currency)
        self.balances_[q.sell_currency] -= q.sell_amount
        self.balances_[q.buy_currency] = self.balances_.get(q.buy_currency, Decimal(0)) + q.buy_amount
        conv = Conversion(self._id("conv"), q.quote_id, q.sell_currency, q.buy_currency, q.sell_amount,
                          q.buy_amount, q.client_rate, "SETTLED")
        self.conversions.append(conv)
        return self._remember(request_id, fp, conv)

    async def balances(self):
        return dict(self.balances_)
