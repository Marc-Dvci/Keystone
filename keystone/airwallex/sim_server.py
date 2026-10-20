"""Replay simulator: the subset of Airwallex's REST API that Keystone calls, served on the same paths
with the same JSON shapes, backed by ``FakeAirwallex``. It lets the whole system, including
``AirwallexClient``, run without sandbox credentials. It is a stand-in, not Airwallex."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid
from decimal import Decimal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..clock import CLOCK
from .base import AirwallexError, CardDetails, Controls
from .fake import FakeAirwallex


def _n(d: Decimal) -> float | int:
    return int(d) if d == d.to_integral_value() else float(d)


def _intent(it) -> dict:
    return {"id": it.id, "amount": _n(it.amount), "currency": it.currency, "status": it.status,
            "merchant_order_id": it.merchant_order_id, "captured_amount": _n(it.captured_amount),
            "metadata": it.metadata}


def _event(t, lifecycle: str) -> dict:
    return {"card_transaction_lifecycle_id": lifecycle, "card_transaction_id": t.id, "card_id": t.card_id,
            "card_transaction_event_id": str(uuid.uuid4()),
            "process_result": "DECLINED" if t.status == "FAILED" else "APPROVED", "failure_reason": t.failure_reason,
            "type": {"AUTHORIZATION": "AUTHORIZATION", "CLEARING": "CLEARING", "REVERSAL": "REVERSAL_AUTH"}.get(
                t.transaction_type, t.transaction_type),
            "transaction_amount": _n(t.amount), "transaction_currency": t.currency,
            "merchant": {"category_code": f"{t.mcc:04d}", "name": t.merchant_name.strip()}}


class RemoteAuthCaller:
    """Calls a remote-authorization URL the way Airwallex does (v2 payload, x-nonce, base64 HMAC)."""

    def __init__(self, url: str, secret: str, http: httpx.AsyncClient | None = None):
        self.url, self.secret = url, secret
        self.http = http or httpx.AsyncClient(timeout=2.5)

    async def __call__(self, req: dict) -> dict:
        nonce = f"{int(time.time() * 1000)}.{uuid.uuid4().hex[:12]}"
        sig = base64.b64encode(hmac.new(self.secret.encode(), nonce.encode(), hashlib.sha256).digest()).decode()
        body = {"version": 2, "card_transaction_event_id": str(uuid.uuid4()), **req}
        try:
            r = await self.http.post(self.url, json=body, headers={"x-nonce": nonce, "x-signature": sig})
            d = r.json()
            if r.status_code != 200 or d.get("card_transaction_event_id") != body["card_transaction_event_id"]:
                return {"response_status": "DECLINED", "status_reason": "REMOTE_AUTH_INVALID_RESPONSE"}
            return d
        except (httpx.HTTPError, ValueError):
            return {"response_status": "DECLINED", "status_reason": "REMOTE_AUTH_TIMEOUT"}  # default_action DECLINED


def build_app(fake: FakeAirwallex | None = None) -> FastAPI:
    fake = fake or FakeAirwallex()
    app = FastAPI(title="Keystone replay simulator (Airwallex API subset)")
    app.state.fake = fake
    lifecycles: dict[str, str] = {}  # lifecycle id -> authorization txn id
    holders: dict[str, dict] = {}

    def err(e: AirwallexError):
        return JSONResponse({"code": e.code, "message": e.message}, status_code=e.status)

    @app.post("/api/v1/authentication/login")
    async def login(request: Request):
        if not request.headers.get("x-client-id") or not request.headers.get("x-api-key"):
            return JSONResponse({"code": "credentials_invalid", "message": "missing"}, status_code=401)
        return JSONResponse({"token": "replay." + uuid.uuid4().hex, "expires_at": "2099-01-01T00:00:00Z"}, 201)

    @app.post("/api/v1/pa/payment_intents/create")
    async def create(b: dict):
        try:
            it = await fake.create_intent(Decimal(str(b["amount"])), b["currency"], b["merchant_order_id"],
                                          b["request_id"], b.get("metadata") or {})
        except AirwallexError as e:
            return err(e)
        return JSONResponse(_intent(it), 201)

    @app.post("/api/v1/pa/payment_intents/{iid}/confirm")
    async def confirm(iid: str, b: dict):
        c = b["payment_method"]["card"]
        opts = (b.get("payment_method_options") or {}).get("card") or {}
        if opts.get("authorization_type") == "pre_auth" and opts.get("auto_capture") is not False:
            return JSONResponse({"code": "validation_error", "message": "pre_auth requires auto_capture false"}, 400)
        try:
            it = await fake.confirm_hold(iid, CardDetails(c["number"], c["expiry_month"], c["expiry_year"], c["cvc"],
                                                          c.get("name", "")), b["request_id"],
                                         opts.get("authorization_type") == "pre_auth")
        except AirwallexError as e:
            return err(e)
        if opts.get("auto_capture", True):
            it = await fake.capture(iid, it.amount, b["request_id"] + "-auto")
        return _intent(it)

    @app.post("/api/v1/pa/payment_intents/{iid}/capture")
    async def capture(iid: str, b: dict):
        try:
            it = await fake.get_intent(iid)
            it = await fake.capture(iid, Decimal(str(b.get("amount", it.amount))), b["request_id"])
        except AirwallexError as e:
            return err(e)
        return _intent(it)

    @app.post("/api/v1/pa/payment_intents/{iid}/cancel")
    async def cancel(iid: str, b: dict):
        try:
            it = await fake.cancel(iid, b.get("cancellation_reason", ""), b["request_id"])
        except AirwallexError as e:
            return err(e)
        d = _intent(it)
        d["cancelled_at"] = CLOCK.now().isoformat()
        d["cancellation_reason"] = b.get("cancellation_reason", "")
        return d

    @app.get("/api/v1/pa/payment_intents/{iid}")
    async def get(iid: str):
        try:
            return _intent(await fake.get_intent(iid))
        except AirwallexError as e:
            return err(e)

    @app.post("/api/v1/pa/refunds/create")
    async def refund(b: dict):
        try:
            r = await fake.refund(b["payment_intent_id"], Decimal(str(b["amount"])), b["request_id"], b.get("reason", ""))
        except AirwallexError as e:
            return err(e)
        return JSONResponse({**r, "amount": _n(r["amount"])}, 201)

    @app.post("/api/v1/simulation/issuing/refund")
    async def sim_refund(b: dict):
        tid = lifecycles.get(b.get("card_transaction_lifecycle_id", ""))
        if tid is None:
            return JSONResponse({"code": "not_found", "message": "lifecycle"}, 404)
        t = await fake.simulate_refund(tid, Decimal(str(b["transaction_amount"])))
        return _event(t, b["card_transaction_lifecycle_id"])

    @app.post("/api/v1/issuing/cardholders/create")
    async def cardholder(b: dict):
        cid = await fake.ensure_cardholder()
        holders[cid] = b
        return JSONResponse({"cardholder_id": cid, "status": "READY", "type": b.get("type")}, 202)

    @app.get("/api/v1/issuing/cardholders/{cid}")
    async def cardholder_get(cid: str):
        return {"cardholder_id": cid, "status": "READY"}

    @app.post("/api/v1/issuing/cards/create")
    async def card_create(b: dict):
        ac = b["authorization_controls"]
        lim = {l["interval"]: Decimal(str(l["amount"])) for l in ac["transaction_limits"]["limits"]}

        def ts(s):
            if not s:
                return None
            from .client import _dt
            return _dt(s)

        controls = Controls(lim["PER_TRANSACTION"], ac["transaction_limits"].get("currency", "USD"),
                            [int(m) for m in ac.get("allowed_merchant_categories") or []],
                            ts(ac.get("active_from")), ts(ac.get("active_to")), lim.get("ALL_TIME"))
        card = await fake.create_card(b["cardholder_id"], controls, b.get("nick_name", ""), b["request_id"])
        return JSONResponse({"card_id": card.id, "card_status": card.status, "nick_name": card.nickname,
                             "card_number": "************" + fake.card_numbers[card.id][-4:],
                             "authorization_controls": ac}, 202)

    @app.get("/api/v1/issuing/cards/{cid}")
    async def card_get(cid: str):
        c = fake.cards[cid]
        return {"card_id": cid, "card_status": c.status, "nick_name": c.nickname}

    @app.get("/api/v1/issuing/cards/{cid}/details")
    async def card_details(cid: str):
        if fake.cards[cid].status != "ACTIVE":
            return JSONResponse({"code": "invalid_argument", "message": "card not active"}, 400)
        d = await fake.card_details(cid)
        return {"card_number": d.number, "cvv": d.cvc, "expiry_month": int(d.expiry_month),
                "expiry_year": int(d.expiry_year), "name_on_card": "KEYSTONE AGENT"}

    @app.post("/api/v1/issuing/cards/{cid}/update")
    async def card_update(cid: str, b: dict):
        if "card_status" in b:
            await fake.set_card_status(cid, b["card_status"])
        return {"card_id": cid, "card_status": fake.cards[cid].status}

    @app.post("/api/v1/simulation/issuing/create")
    async def sim_auth(b: dict):
        number = b.get("card_number")
        card_id = b.get("card_id")
        if number:
            card_id = next((k for k, v in fake.card_numbers.items() if v == number), None)
        if card_id is None:
            return JSONResponse({"code": "invalid_argument", "message": "card not owned by account"}, 400)
        t = await fake.simulate_authorization(card_id, number or fake.card_numbers[card_id],
                                              Decimal(str(b["transaction_amount"])), b["transaction_currency"],
                                              int(b.get("merchant_category_code") or 0), b.get("merchant_info", ""))
        if b.get("transaction_failure_reason") and t.status != "FAILED":
            await fake.simulate_reverse(t.id, t.amount)
            t.status, t.failure_reason = "FAILED", b["transaction_failure_reason"]
        lc = str(uuid.uuid4())
        lifecycles[lc] = t.id
        return JSONResponse(_event(t, lc), 201)

    async def lifecycle_op(lc: str, op: str, b: dict):
        tid = lifecycles.get(lc)
        if tid is None:
            return JSONResponse({"code": "not_found", "message": "lifecycle"}, 404)
        auth = fake.txns[tid]
        amt = Decimal(str(b["transaction_amount"])) if b.get("transaction_amount") is not None else auth.amount
        try:
            t = await (fake.simulate_capture if op == "capture" else fake.simulate_reverse)(tid, amt)
        except AirwallexError as e:
            return err(e)
        return _event(t, lc)

    @app.post("/api/v1/simulation/issuing/card_transaction_lifecycles/{lc}/capture")
    async def sim_capture(lc: str, request: Request):
        return await lifecycle_op(lc, "capture", await _body(request))

    @app.post("/api/v1/simulation/issuing/card_transaction_lifecycles/{lc}/reverse")
    async def sim_reverse(lc: str, request: Request):
        return await lifecycle_op(lc, "reverse", await _body(request))

    @app.get("/api/v1/issuing/transactions")
    async def txns(card_id: str):
        items = []
        for t in await fake.card_transactions(card_id):
            items.append({"transaction_id": t.id, "card_id": t.card_id, "transaction_type": t.transaction_type,
                          "status": t.status, "failure_reason": t.failure_reason, "transaction_amount": _n(t.amount),
                          "transaction_currency": t.currency,
                          "merchant": {"category_code": f"{t.mcc:04d}", "name": t.merchant_name.strip()}})
        return {"has_more": False, "items": items}

    @app.post("/api/v1/fx/quotes/create")
    async def quote(b: dict):
        try:
            q = await fake.quote(b["sell_currency"], b["buy_currency"], Decimal(str(b["buy_amount"])), b["validity"])
        except AirwallexError as e:
            return err(e)
        return JSONResponse({"quote_id": q.quote_id, "sell_currency": q.sell_currency, "buy_currency": q.buy_currency,
                             "buy_amount": _n(q.buy_amount), "sell_amount": _n(q.sell_amount),
                             "client_rate": float(q.client_rate), "valid_to_at": q.valid_to.isoformat(),
                             "validity": b["validity"], "usage": "SINGLE_USE"}, 201)

    @app.post("/api/v1/fx/conversions/create")
    async def convert(b: dict):
        q = fake.quotes.get(b.get("quote_id", ""))
        if q is None:
            return JSONResponse({"code": "invalid_quote_id", "message": "unknown quote"}, 400)
        try:
            c = await fake.convert(q, b["request_id"])
        except AirwallexError as e:
            return err(e)
        return JSONResponse({"conversion_id": c.id, "status": c.status, "sell_currency": c.sell_currency,
                             "buy_currency": c.buy_currency, "sell_amount": _n(c.sell_amount),
                             "buy_amount": _n(c.buy_amount), "client_rate": float(c.client_rate),
                             "request_id": b["request_id"]}, 201)

    @app.get("/api/v1/fx/rates/current")
    async def rate(sell_currency: str, buy_currency: str):
        return {"rate": float(await fake.spot(sell_currency, buy_currency))}

    @app.get("/api/v1/balances/current")
    async def balances():
        return [{"account_type": "cash", "currency": k, "available_amount": _n(v), "total_amount": _n(v)}
                for k, v in (await fake.balances()).items()]

    @app.post("/sim/remote_auth")
    async def set_remote_auth(b: dict):
        """Configure the remote-authorization URL, like POST /issuing/config/update remote_auth."""
        fake.remote_auth = RemoteAuthCaller(b["url"], b["secret"]) if b.get("enabled") else None
        return {"ok": True}

    return app


async def _body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    import json
    return json.loads(raw)


app = build_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("AWX_SIM_PORT", "8090")))
