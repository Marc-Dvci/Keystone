"""Airwallex REST client (sandbox by default). Field names follow the API reference; see
docs/AIRWALLEX_CALLS.md for each call, its fields and the sandbox behaviour it relies on.

The same client talks to the real sandbox (https://api.sandbox.airwallex.com) and to Keystone's
replay simulator (``keystone.airwallex.sim_server``), so every request shape is exercised in tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from ..money import quantize
from .base import (AirwallexError, Card, CardDetails, CardTxn, Controls, Conversion, Intent, Quote, RemoteAuth)

SANDBOX = "https://api.sandbox.airwallex.com"


def _ts(d: datetime | None) -> str | None:
    return None if d is None else d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


def _num(d: Decimal) -> float | int:
    # Airwallex takes JSON numbers in major units. Decimals are quantized first, so the float is exact
    # to the currency's minor unit.
    return int(d) if d == d.to_integral_value() else float(d)


class AirwallexClient:
    def __init__(self, client_id: str | None = None, api_key: str | None = None, base_url: str | None = None,
                 http: httpx.AsyncClient | None = None, cardholder_email: str = "agent@keystone.example"):
        self.client_id = client_id or os.environ.get("AWX_CLIENT_ID", "")
        self.api_key = api_key or os.environ.get("AWX_API_KEY", "")
        self.base = (base_url or os.environ.get("AWX_BASE_URL") or SANDBOX).rstrip("/")
        self.mode = "live" if self.base == SANDBOX else "replay"
        self.http = http or httpx.AsyncClient(timeout=20)
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = asyncio.Lock()
        self.cardholder_id: str | None = os.environ.get("AWX_CARDHOLDER_ID")
        self.cardholder_email = cardholder_email
        self.remote_auth: RemoteAuth | None = None
        self.calls: list[dict] = []
        self.remote_auth_enabled = os.environ.get("AWX_REMOTE_AUTH_ENABLED") == "1"

    # ------------------------------------------------------------------ transport
    async def _auth(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            r = await self.http.post(f"{self.base}/api/v1/authentication/login",
                                     headers={"x-client-id": self.client_id, "x-api-key": self.api_key})
            if r.status_code not in (200, 201):
                raise AirwallexError(_code(r), r.text[:300], r.status_code)
            self._token = r.json()["token"]
            self._token_exp = time.time() + 30 * 60
            return self._token

    async def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        tok = await self._auth()
        t = time.perf_counter()
        for attempt in range(4):
            r = await self.http.request(method, f"{self.base}/api/v1{path}", json=body, params=params,
                                        headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 429:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            break
        self.calls.append({"method": method, "path": path, "status": r.status_code,
                           "ms": int((time.perf_counter() - t) * 1000)})
        if r.status_code >= 400:
            raise AirwallexError(_code(r), _msg(r), r.status_code, _json(r))
        return _json(r)

    # ------------------------------------------------------------------ acquiring
    @staticmethod
    def _intent(d: dict) -> Intent:
        return Intent(d["id"], Decimal(str(d["amount"])), d["currency"], d["status"], d.get("merchant_order_id", ""),
                      Decimal(str(d.get("captured_amount") or 0)), d.get("metadata") or {}, d)

    async def create_intent(self, amount, currency, merchant_order_id, request_id, metadata):
        d = await self._call("POST", "/pa/payment_intents/create", {
            "amount": _num(quantize(amount, currency)), "currency": currency, "merchant_order_id": merchant_order_id[:64],
            "request_id": request_id, "metadata": {k: str(v)[:500] for k, v in metadata.items()}})
        return self._intent(d)

    async def confirm_hold(self, intent_id, card: CardDetails, request_id, pre_auth):
        opts: dict = {"auto_capture": False}
        if pre_auth:
            opts["authorization_type"] = "pre_auth"
        d = await self._call("POST", f"/pa/payment_intents/{intent_id}/confirm", {
            "request_id": request_id,
            "payment_method": {"type": "card", "card": {"number": card.number, "expiry_month": card.expiry_month,
                                                         "expiry_year": card.expiry_year, "cvc": card.cvc,
                                                         "name": card.name}},
            "payment_method_options": {"card": opts}})
        it = self._intent(d)
        if it.status != "REQUIRES_CAPTURE":
            raise AirwallexError("not_held", f"intent is {it.status} after confirm", 400, d)
        return it

    async def capture(self, intent_id, amount, request_id):
        it = await self.get_intent(intent_id)
        await self._call("POST", f"/pa/payment_intents/{intent_id}/capture",
                         {"request_id": request_id, "amount": _num(quantize(amount, it.currency))})
        # The capture response body is not documented; re-read the intent.
        return await self.get_intent(intent_id)

    async def cancel(self, intent_id, reason, request_id):
        d = await self._call("POST", f"/pa/payment_intents/{intent_id}/cancel",
                             {"request_id": request_id, "cancellation_reason": reason[:120]})
        return self._intent(d)

    async def get_intent(self, intent_id):
        return self._intent(await self._call("GET", f"/pa/payment_intents/{intent_id}"))

    async def refund(self, intent_id, amount, request_id, reason="keystone_compensation"):
        return await self._call("POST", "/pa/refunds/create", {"payment_intent_id": intent_id, "request_id": request_id,
                                                               "amount": _num(amount), "reason": reason})

    # ------------------------------------------------------------------ issuing
    async def ensure_cardholder(self):
        if self.cardholder_id:
            return self.cardholder_id
        d = await self._call("POST", "/issuing/cardholders/create", {
            "type": "INDIVIDUAL", "email": self.cardholder_email,
            "individual": {"name": {"first_name": "Keystone", "last_name": "Agent"}, "date_of_birth": "1985-04-12",
                           "express_consent_obtained": "yes",
                           "address": {"city": "San Francisco", "country": "US", "line1": "1 Market St",
                                       "postcode": "94105", "state": "CA"}}})
        self.cardholder_id = d["cardholder_id"]
        for _ in range(20):
            if d.get("status") == "READY":
                break
            if d.get("status") == "PENDING":
                try:
                    await self._call("POST", f"/simulation/issuing/cardholders/{self.cardholder_id}/pass_review")
                except AirwallexError:
                    pass
            await asyncio.sleep(0.5)
            d = await self._call("GET", f"/issuing/cardholders/{self.cardholder_id}")
        return self.cardholder_id

    @staticmethod
    def _controls_body(c: Controls) -> dict:
        limits = [{"amount": _num(c.per_transaction_limit), "interval": "PER_TRANSACTION"}]
        if c.all_time_limit is not None:
            limits.append({"amount": _num(c.all_time_limit), "interval": "ALL_TIME"})
        body = {"allowed_transaction_count": "SINGLE",
                "transaction_limits": {"currency": c.currency, "limits": limits},
                "allowed_currencies": [c.currency],
                "allowed_merchant_categories": [f"{m:04d}" for m in c.allowed_mccs]}
        if c.active_from:
            body["active_from"] = _ts(c.active_from)
        if c.active_to:
            body["active_to"] = _ts(c.active_to)
        return body

    async def create_card(self, cardholder_id, controls: Controls, nickname, request_id):
        d = await self._call("POST", "/issuing/cards/create", {
            "request_id": request_id, "cardholder_id": cardholder_id, "is_personalized": False,
            "form_factor": "VIRTUAL", "created_by": "Keystone coordinator", "nick_name": nickname[:40],
            "program": {"purpose": "COMMERCIAL"}, "purpose": "TRAVEL_EXPENSES",
            "authorization_controls": self._controls_body(controls)})
        card_id = d["card_id"]
        status = d.get("card_status", "PENDING")
        for _ in range(20):
            if status == "ACTIVE":
                break
            await asyncio.sleep(0.5)
            status = (await self._call("GET", f"/issuing/cards/{card_id}")).get("card_status")
        return Card(card_id, status, controls, nickname, d)

    async def card_details(self, card_id):
        d = await self._call("GET", f"/issuing/cards/{card_id}/details")
        return CardDetails(d["card_number"], f"{int(d['expiry_month']):02d}", str(d["expiry_year"]), d["cvv"],
                           d.get("name_on_card") or "Keystone Agent")

    async def set_card_status(self, card_id, status):
        await self._call("POST", f"/issuing/cards/{card_id}/update", {"card_status": status})
        d = await self._call("GET", f"/issuing/cards/{card_id}")
        return Card(card_id, d.get("card_status", status), None, d.get("nick_name", ""), d)  # type: ignore[arg-type]

    @staticmethod
    def _txn(d: dict, card_id: str, fallback_type: str) -> CardTxn:
        ok = d.get("process_result", "APPROVED") == "APPROVED"
        merchant = d.get("merchant") or {}
        return CardTxn(d.get("card_transaction_lifecycle_id") or d.get("card_transaction_id") or d.get("transaction_id", ""),
                       d.get("card_id", card_id), d.get("type", fallback_type), "PENDING" if ok else "FAILED",
                       Decimal(str(d.get("transaction_amount", 0))), d.get("transaction_currency", ""),
                       int(merchant.get("category_code") or 0), merchant.get("name", ""), d.get("failure_reason"), d)

    async def simulate_authorization(self, card_id, card_number, amount, currency, mcc, merchant_name):
        body = {"card_number": card_number, "transaction_amount": _num(quantize(amount, currency)),
                "transaction_currency": currency, "merchant_category_code": f"{mcc:04d}",
                "merchant_info": _merchant_info(merchant_name)}
        relay = None
        if self.remote_auth is not None and not self.remote_auth_enabled:
            # Remote authorization is not enabled on this sandbox account, so the decision Airwallex would
            # request from the basket ledger is requested here, with the same v2 payload, before the
            # authorization reaches Airwallex. A decline is recorded on Airwallex as TRANSACTION_BLOCKED.
            relay = await self.remote_auth({
                "version": 2, "card_id": card_id, "card_number_sha256": hashlib.sha256(card_number.encode()).hexdigest(),
                "card_transaction_event_id": str(uuid.uuid4()), "transaction_type": "AUTHORIZATION",
                "transaction_amount": str(quantize(amount, currency)), "transaction_currency": currency,
                "merchant": {"name": merchant_name, "category_code": f"{mcc:04d}"}})
            if relay.get("response_status") != "AUTHORIZED":
                body["transaction_failure_reason"] = "TRANSACTION_BLOCKED"
        d = await self._call("POST", "/simulation/issuing/create", body)
        t = self._txn(d, card_id, "AUTHORIZATION")
        if relay is not None and relay.get("response_status") != "AUTHORIZED":
            t.status, t.failure_reason = "FAILED", relay.get("status_reason") or "REMOTE_AUTH_DECLINED"
        return t

    async def _lifecycle(self, txn_id: str, op: str, amount: Decimal | None) -> dict:
        body = {} if amount is None else {"transaction_amount": _num(amount)}
        try:
            return await self._call("POST", f"/simulation/issuing/card_transaction_lifecycles/{txn_id}/{op}", body)
        except AirwallexError as e:
            if e.status != 404:
                raise
            return await self._call("POST", f"/simulation/issuing/{txn_id}/{op}", body)

    async def simulate_capture(self, txn_id, amount):
        d = await self._lifecycle(txn_id, "capture", amount)
        t = self._txn(d, d.get("card_id", ""), "CLEARING")
        t.transaction_type = d.get("type", "CLEARING")
        t.status = "APPROVED" if d.get("process_result", "APPROVED") == "APPROVED" else "FAILED"
        return t

    async def simulate_reverse(self, txn_id, amount):
        d = await self._lifecycle(txn_id, "reverse", amount)
        t = self._txn(d, d.get("card_id", ""), "REVERSAL_AUTH")
        t.status = "APPROVED"
        return t

    async def card_transactions(self, card_id):
        d = await self._call("GET", "/issuing/transactions", params={"card_id": card_id, "page_size": 100})
        out = []
        for x in d.get("items", []):
            m = x.get("merchant") or {}
            out.append(CardTxn(x.get("transaction_id", ""), card_id, x.get("transaction_type", ""), x.get("status", ""),
                               Decimal(str(x.get("transaction_amount", 0))), x.get("transaction_currency", ""),
                               int(m.get("category_code") or 0), m.get("name", ""), x.get("failure_reason"), x))
        return out

    # ------------------------------------------------------------------ fx
    async def quote(self, sell_currency, buy_currency, buy_amount, validity):
        d = await self._call("POST", "/fx/quotes/create", {"sell_currency": sell_currency, "buy_currency": buy_currency,
                                                           "buy_amount": _num(quantize(buy_amount, buy_currency)),
                                                           "validity": validity})
        return Quote(d["quote_id"], d["sell_currency"], d["buy_currency"], Decimal(str(d["buy_amount"])),
                     Decimal(str(d["sell_amount"])), Decimal(str(d["client_rate"])), _dt(d["valid_to_at"]), d)

    async def convert(self, quote: Quote, request_id):
        d = await self._call("POST", "/fx/conversions/create", {
            "quote_id": quote.quote_id, "sell_currency": quote.sell_currency, "buy_currency": quote.buy_currency,
            "buy_amount": str(quote.buy_amount), "request_id": request_id})
        return Conversion(d.get("conversion_id", d.get("id", "")), quote.quote_id, d["sell_currency"], d["buy_currency"],
                          Decimal(str(d["sell_amount"])), Decimal(str(d["buy_amount"])), Decimal(str(d["client_rate"])),
                          d.get("status", ""), d)

    async def spot(self, sell_currency, buy_currency):
        d = await self._call("GET", "/fx/rates/current", params={"sell_currency": sell_currency,
                                                                 "buy_currency": buy_currency})
        return Decimal(str(d.get("rate") or d.get("client_rate")))

    async def balances(self):
        d = await self._call("GET", "/balances/current")
        return {b["currency"]: Decimal(str(b.get("available_amount", 0))) for b in d}


def _merchant_info(name: str) -> str:
    # The reference says merchant_info is exactly 43 characters (the default is shorter); pad to 43.
    return (name[:43]).ljust(43)


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text[:500]}


def _code(r: httpx.Response) -> str:
    j = _json(r)
    return j.get("code", f"http_{r.status_code}") if isinstance(j, dict) else f"http_{r.status_code}"


def _msg(r: httpx.Response) -> str:
    j = _json(r)
    return j.get("message", "") if isinstance(j, dict) else ""
