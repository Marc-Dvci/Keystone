"""Merchant side of Keystone: a FastAPI service any merchant on Airwallex Payments can run.

Endpoints (all TAP-verified):
  GET  /catalog                 agent-browser-auth   offers for an agent to compare
  POST /keystone/prepare        agent-payer-auth     reserve stock, hold the card, return a signed hold receipt
  POST /keystone/commit         agent-payer-auth     capture the hold, return a signed capture receipt
  POST /keystone/abort          agent-payer-auth     void the hold, release stock, return a signed void receipt
  GET  /.well-known/keystone.json                    merchant id, receipt key, category, currency

A hold is a vote; a capture is the commit; a void is a free abort. A reservation that reaches its
deadline without a commit is voided by the merchant on its own (presumed abort).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import checkout as checkout_page
from . import rules, tap
from .airwallex.base import AirwallexError, CardDetails
from .canonical import canonical, digest
from .clock import CLOCK
from .events import BUS, Bus
from .mandate import Offer
from .money import quantize
from .network import Declined, Network

NS = uuid.UUID("6b0f3c2e-8d4c-4b8e-9f51-5a1e2d7c9b10")


def rid(*parts: str) -> str:
    """Deterministic request_id per operation, so a retry is recognised by Airwallex as the same call."""
    return str(uuid.uuid5(NS, "|".join(parts)))


def sign_receipt(key: Ed25519PrivateKey, key_id: str, receipt: dict) -> dict:
    sig = base64.b64encode(key.sign(canonical(receipt))).decode()
    return {"receipt": receipt, "signature": sig, "key_id": key_id}


def verify_receipt(public_b64: str, signed: dict) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(
            base64.b64decode(signed["signature"]), canonical(signed["receipt"]))
        return True
    except Exception:
        return False


@dataclass
class Hold:
    basket_id: str
    leg_id: str
    offer_id: str
    quantity: int
    amount: Decimal
    currency: str
    intent_id: str
    mandate_digest: str
    reserved_until: datetime
    authorized_at: datetime
    state: str = "HELD"  # PENDING_CHECKOUT | HELD | CAPTURED | VOIDED | REFUNDED
    receipts: dict = field(default_factory=dict)
    voided_at: datetime | None = None
    abort_requested_at: datetime | None = None


@dataclass
class Fault:
    phase: str  # prepare | commit | abort
    mode: str  # error | timeout | after_effect_error
    remaining: int = 1


class MerchantService:
    def __init__(self, merchant_id: str, name: str, authority: str, mcc: int, currency: str, offers: list[Offer],
                 acquirer, network: Network, directory: tap.KeyDirectory, reservation_ttl: timedelta,
                 pre_auth: bool = False, bus: Bus = BUS, seed: bytes | None = None, checkout: str = "server",
                 awx_env: str = "demo"):
        self.id = merchant_id
        self.name = name
        self.authority = authority
        self.mcc = mcc
        self.currency = currency
        self.offers = {o.offer_id: o for o in offers}
        self.initial = {o.offer_id: o.available for o in offers}
        self.acq = acquirer
        self.net = network
        self.directory = directory
        self.nonces = tap.MemoryNonceStore()
        self.ttl = reservation_ttl
        self.pre_auth = pre_auth
        # "server": the merchant confirms the hold through the API (replay simulator).
        # "element": the buyer's browser confirms it in Airwallex's Card Element (Airwallex sandbox).
        self.checkout = checkout
        self.awx_env = awx_env
        self.checkout_tokens: dict[str, tuple[str, str, str]] = {}
        self.bus = bus
        self.key = Ed25519PrivateKey.from_private_bytes(
            hashlib.sha256(seed or merchant_id.encode()).digest())
        self.key_id = f"{merchant_id}-receipts-1"
        self.holds: dict[tuple[str, str, str], Hold] = {}
        self.aborted: set[tuple[str, str, str]] = set()
        self.faults: list[Fault] = []
        self.rogue = False  # tries to capture after an abort (fault injection)
        self.sell_on_prepare: dict[str, int] = {}
        self._tasks: set = set()  # another buyer takes this stock as the agent arrives
        self.latency_s = 0.0
        self.app = self._build()

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    # ------------------------------------------------------------------ helpers
    def _emit(self, kind: str, **data: Any) -> None:
        self.bus.emit(kind, self.id, **data)

    async def _verify(self, request: Request, body: bytes, tag: str) -> tap.Verified:
        try:
            v = await tap.verify_request(request.method, self.authority, request.url.path, dict(request.headers), body,
                                         self.directory, self.nonces, require_tag=tag)
        except tap.TapError as e:
            self._emit("tap.rejected", path=request.url.path, code=e.code, detail=e.detail)
            raise HTTPException(401, {"error": "tap_" + e.code, "detail": e.detail})
        self._emit("tap.verified", path=request.url.path, key_id=v.key_id, tag=v.tag, nonce=v.nonce[:10])
        return v

    def _fault(self, phase: str) -> Fault | None:
        for f in self.faults:
            if f.phase == phase and f.remaining > 0:
                f.remaining -= 1
                return f
        return None

    def _receipt(self, kind: str, h: Hold, **extra: Any) -> dict:
        r = {"type": f"keystone.{kind}/1", "merchant_id": self.id, "merchant_name": self.name,
             "basket_id": h.basket_id, "leg_id": h.leg_id, "offer_id": h.offer_id, "quantity": h.quantity,
             "amount": str(h.amount), "currency": h.currency, "intent_id": h.intent_id,
             "mandate_digest": h.mandate_digest, "issued_at": CLOCK.now().isoformat(timespec="seconds"), **extra}
        return sign_receipt(self.key, self.key_id, r)

    async def sweep(self) -> None:
        """Presumed abort: void every reservation whose deadline passed without a commit."""
        now = CLOCK.now()
        for h in list(self.holds.values()):
            if h.state in ("HELD", "PENDING_CHECKOUT") and now > h.reserved_until:
                await self._void(h, "reservation_expired")

    async def _void(self, h: Hold, reason: str) -> None:
        h.abort_requested_at = h.abort_requested_at or CLOCK.now()
        await self.net.void(h.intent_id, reason, rid(h.intent_id, "cancel"))
        h.state = "VOIDED"
        h.voided_at = CLOCK.now()
        self.offers[h.offer_id].available += h.quantity
        chk = rules.check_reversal_latency(h.abort_requested_at, h.voided_at, CLOCK.now())
        h.receipts["void"] = self._receipt("void", h, reason=reason, reversal=chk.to_json())
        self._emit("merchant.voided", basket_id=h.basket_id, leg_id=h.leg_id, intent=h.intent_id, reason=reason,
                   amount=str(h.amount), currency=h.currency, check=chk.to_json())

    # ------------------------------------------------------------------ phases
    async def prepare(self, body: dict, v: tap.Verified) -> dict:
        key = (body["basket_id"], body["leg_id"], body["offer_id"])
        if key in self.aborted:
            raise HTTPException(409, {"error": "leg_aborted"})
        if key in self.holds:
            h = self.holds[key]
            if h.state == "HELD":
                return h.receipts["hold"]
            if h.state == "PENDING_CHECKOUT":
                return await self._finish_checkout(h, v)
            raise HTTPException(409, {"error": f"leg_{h.state.lower()}"})
        offer = self.offers.get(body["offer_id"])
        if offer is None:
            raise HTTPException(404, {"error": "unknown_offer"})
        if offer.offer_id in self.sell_on_prepare:
            offer.available = self.sell_on_prepare.pop(offer.offer_id)
            self._emit("merchant.inventory_taken", offer_id=offer.offer_id, available=offer.available,
                       note="another buyer took the stock moments before this request")
        qty = int(body["quantity"])
        amount = quantize(body["amount"], body["currency"])
        if body["currency"] != offer.currency:
            raise HTTPException(409, {"error": "currency_mismatch", "currency": offer.currency})
        price = quantize(offer.total(qty), offer.currency)
        if amount != price:
            raise HTTPException(409, {"error": "price_changed", "amount": str(price), "currency": offer.currency})
        if offer.available < qty:
            self._emit("merchant.sold_out", basket_id=key[0], leg_id=key[1], offer_id=offer.offer_id)
            raise HTTPException(409, {"error": "sold_out", "available": offer.available})
        commit_by = datetime.fromisoformat(body["commit_by"])
        now = CLOCK.now()
        window = rules.check_hold_window(self.mcc, now, commit_by, extended=self.pre_auth)
        if not window.ok:
            raise HTTPException(422, {"error": "commit_deadline_beyond_hold_validity", "check": window.to_json()})
        offer.available -= qty  # reserve before the hold so two agents cannot both get the last room
        try:
            intent = await self.acq.create_intent(
                amount, offer.currency, f"{self.id}-{key[0][:12]}-{key[1]}", rid(self.id, *key, "create"),
                {"keystone_basket": key[0], "keystone_leg": key[1], "mandate_digest": body["mandate_digest"],
                 "tap_key_id": v.key_id, "merchant": self.id})
            card = CardDetails(**body["payment"]["card"])
            if self.checkout == "element":
                await self.net.authorize_issuer(intent, card, self.mcc, self.name)
            else:
                await self.net.authorize(intent, card, self.mcc, self.name, rid(intent.id, "confirm"), self.pre_auth)
        except Declined as d:
            offer.available += qty
            self._emit("merchant.declined", basket_id=key[0], leg_id=key[1], reason=d.reason)
            raise HTTPException(402, {"error": "declined", "reason": d.reason})
        except AirwallexError as e:
            offer.available += qty
            raise HTTPException(502, {"error": "acquirer_error", "code": e.code, "message": e.message})
        reserved_until = min(commit_by, now + self.ttl)
        h = Hold(key[0], key[1], offer.offer_id, qty, amount, offer.currency, intent.id, body["mandate_digest"],
                 reserved_until, now)
        if self.checkout == "element":
            h.state = "PENDING_CHECKOUT"
            h.receipts["pending"] = {"title": offer.title, "attrs": offer.attrs,
                                     "client_secret": intent.raw.get("client_secret", ""), "window": window.to_json()}
            self.holds[key] = h
            token = hashlib.sha256(f"{intent.id}|{self.id}".encode()).hexdigest()[:24]
            self.checkout_tokens[token] = key
            self._emit("merchant.checkout_ready", basket_id=key[0], leg_id=key[1], intent=intent.id)
            return self._needs_checkout(token, intent.id)
        valid_to = now + rules.validity(self.mcc, extended=self.pre_auth)
        h.receipts["hold"] = self._receipt(
            "hold", h, title=offer.title, attrs=offer.attrs, reserved_until=reserved_until.isoformat(timespec="seconds"),
            authorization_valid_to=valid_to.isoformat(timespec="seconds"),
            terms="void before capture is free; capture only on a keystone commit for this basket",
            tap={"key_id": v.key_id, "nonce": v.nonce, "tag": v.tag}, checks=[window.to_json()])
        self.holds[key] = h
        self._emit("merchant.held", basket_id=key[0], leg_id=key[1], intent=intent.id, amount=str(amount),
                   currency=offer.currency, reserved_until=reserved_until.isoformat(timespec="seconds"),
                   title=offer.title)
        return h.receipts["hold"]

    def _needs_checkout(self, token: str, intent_id: str) -> JSONResponse:
        return JSONResponse({"status": "needs_checkout", "checkout_url": f"http://{self.authority}/checkout/{token}",
                             "intent_id": intent_id}, status_code=202)

    async def _finish_checkout(self, h: Hold, v: tap.Verified):
        """The buyer's browser confirmed (or not) in Airwallex's Card Element; the intent says which."""
        it = await self.acq.get_intent(h.intent_id)
        if it.status == "REQUIRES_PAYMENT_METHOD":
            token = next(t for t, k in self.checkout_tokens.items() if k == (h.basket_id, h.leg_id, h.offer_id))
            return self._needs_checkout(token, h.intent_id)
        if it.status != "REQUIRES_CAPTURE":
            await self._void(h, f"checkout_{it.status.lower()}")
            raise HTTPException(402, {"error": "declined", "reason": f"intent {it.status}"})
        p = h.receipts.pop("pending")
        h.state = "HELD"
        h.authorized_at = CLOCK.now()
        valid_to = h.authorized_at + rules.validity(self.mcc, extended=self.pre_auth)
        h.receipts["hold"] = self._receipt(
            "hold", h, title=p["title"], attrs=p["attrs"], reserved_until=h.reserved_until.isoformat(timespec="seconds"),
            authorization_valid_to=valid_to.isoformat(timespec="seconds"),
            terms="void before capture is free; capture only on a keystone commit for this basket",
            tap={"key_id": v.key_id, "nonce": v.nonce, "tag": v.tag}, checks=[p["window"]],
            confirmed_in="Airwallex Card Element")
        self._emit("merchant.held", basket_id=h.basket_id, leg_id=h.leg_id, intent=h.intent_id, amount=str(h.amount),
                   currency=h.currency, reserved_until=h.reserved_until.isoformat(timespec="seconds"), title=p["title"],
                   via="Airwallex Card Element")
        return h.receipts["hold"]

    async def commit(self, body: dict, v: tap.Verified) -> dict:
        h = self.holds.get((body["basket_id"], body["leg_id"], body["offer_id"]))
        if h is None:
            raise HTTPException(404, {"error": "no_hold"})
        if h.state == "CAPTURED":
            return h.receipts["capture"]
        if h.state == "VOIDED":
            raise HTTPException(409, {"error": "leg_voided"})
        amount = quantize(body.get("amount", h.amount), h.currency)
        checks = rules.check_capture(h.amount, amount, False, False, h.authorized_at, CLOCK.now(), self.mcc,
                                     extended=self.pre_auth)
        bad = [c for c in checks if not c.ok]
        if bad:
            raise HTTPException(422, {"error": "rule_failed", "checks": [c.to_json() for c in checks]})
        try:
            await self.net.capture(h.intent_id, amount, rid(h.intent_id, "capture"))
        except AirwallexError as e:
            raise HTTPException(502, {"error": "acquirer_error", "code": e.code, "message": e.message})
        h.state = "CAPTURED"
        h.receipts["capture"] = self._receipt("capture", h, captured=str(amount), commit_digest=body.get("commit_digest"),
                                              checks=[c.to_json() for c in checks])
        self._emit("merchant.captured", basket_id=h.basket_id, leg_id=h.leg_id, intent=h.intent_id,
                   amount=str(amount), currency=h.currency)
        return h.receipts["capture"]

    async def abort(self, body: dict, v: tap.Verified) -> dict:
        h = self.holds.get((body["basket_id"], body["leg_id"], body["offer_id"]))
        if h is None:
            # Abort before any hold: record it so a late prepare for this leg is refused.
            self.aborted.add((body["basket_id"], body["leg_id"], body["offer_id"]))
            return {"receipt": {"type": "keystone.void/1", "merchant_id": self.id, "basket_id": body["basket_id"],
                                "leg_id": body["leg_id"], "offer_id": body["offer_id"], "note": "no hold existed"}, "signature": "", "key_id": ""}
        if h.state == "CAPTURED":
            raise HTTPException(409, {"error": "leg_captured"})
        if h.state in ("VOIDED", "REFUNDED"):
            return h.receipts.get("void") or h.receipts.get("refund")
        h.abort_requested_at = CLOCK.now()
        await self._void(h, body.get("reason", "basket_aborted"))
        if self.rogue:
            t = asyncio.get_running_loop().create_task(self._rogue_capture(h))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return h.receipts["void"]

    async def refund(self, body: dict, v: tap.Verified) -> dict:
        """After capture there is no free undo: the merchant's refund policy decides."""
        h = self.holds.get((body["basket_id"], body["leg_id"], body["offer_id"]))
        if h is None or h.state != "CAPTURED":
            raise HTTPException(409, {"error": "not_captured"})
        if not self.offers[h.offer_id].attrs.get("refundable", False):
            self._emit("merchant.refund_refused", basket_id=h.basket_id, leg_id=h.leg_id, amount=str(h.amount),
                       currency=h.currency, reason="non_refundable")
            raise HTTPException(409, {"error": "non_refundable", "amount": str(h.amount), "currency": h.currency})
        await self.net.refund(h.intent_id, h.amount, rid(h.intent_id, "refund"))
        h.state = "REFUNDED"
        self.offers[h.offer_id].available += h.quantity
        h.receipts["refund"] = self._receipt("refund", h, reason=body.get("reason", ""))
        self._emit("merchant.refunded", basket_id=h.basket_id, leg_id=h.leg_id, amount=str(h.amount), currency=h.currency)
        return h.receipts["refund"]

    async def _rogue_capture(self, h: Hold) -> None:
        """Fault injection: a merchant that captures after it was told to abort."""
        await asyncio.sleep(0.3)
        try:
            await self.net.capture(h.intent_id, h.amount, rid(h.intent_id, "rogue-capture"))
            self._emit("merchant.rogue_capture_succeeded", basket_id=h.basket_id, leg_id=h.leg_id)
        except AirwallexError as e:
            chk = rules.Check("visa.1.7.7.2.no_deposit_after_reversal", True,
                              f"capture after full reversal refused by Airwallex: {e.code}")
            self._emit("merchant.rogue_capture_refused", basket_id=h.basket_id, leg_id=h.leg_id, intent=h.intent_id,
                       code=e.code, message=e.message, check=chk.to_json())

    # ------------------------------------------------------------------ app
    def _build(self) -> FastAPI:
        app = FastAPI(title=f"{self.name} (Keystone merchant)")
        svc = self

        @app.get("/.well-known/keystone.json")
        async def well_known():
            return {"merchant_id": svc.id, "name": svc.name, "mcc": svc.mcc, "currency": svc.currency,
                    "receipt_key": {"key_id": svc.key_id, "alg": "ed25519", "public_key": svc.public_b64},
                    "keystone": "1", "phases": ["prepare", "commit", "abort"], "tap_tags": sorted(tap.TAP_TAGS)}

        @app.get("/checkout/{token}")
        async def checkout(token: str, request: Request):
            await svc._verify(request, b"", tap.TAG_PAY)
            key = svc.checkout_tokens.get(token)
            h = svc.holds.get(key) if key else None
            if h is None or h.state != "PENDING_CHECKOUT":
                raise HTTPException(404, {"error": "no_pending_checkout"})
            p = h.receipts["pending"]
            return HTMLResponse(checkout_page.page_html(h.intent_id, p["client_secret"], h.currency, svc.awx_env,
                                                        svc.pre_auth, f"{svc.name}: {p['title']}", str(h.amount)))

        @app.get("/catalog")
        async def catalog(request: Request):
            await svc._verify(request, b"", tap.TAG_BROWSE)
            await svc.sweep()
            return {"merchant_id": svc.id, "offers": [o.model_dump(mode="json") for o in svc.offers.values()]}

        async def phase(request: Request, name: str):
            body = await request.body()
            v = await svc._verify(request, body, tap.TAG_PAY)
            data = json.loads(body)
            if data.get("phase") != name:
                raise HTTPException(400, {"error": "phase_mismatch", "detail": "the signed body names another phase"})
            if svc.latency_s:
                await asyncio.sleep(svc.latency_s)
            f = svc._fault(name)
            if f and f.mode in ("error", "timeout"):
                svc._emit("fault.injected", phase=name, mode=f.mode, leg_id=data.get("leg_id"))
                if f.mode == "timeout":
                    await asyncio.sleep(30)
                raise HTTPException(503, {"error": "merchant_unavailable"})
            await svc.sweep()
            out = await getattr(svc, name)(data, v)
            if isinstance(out, JSONResponse):
                return out
            if f and f.mode == "after_effect_error":
                svc._emit("fault.injected", phase=name, mode=f.mode, leg_id=data.get("leg_id"))
                raise HTTPException(503, {"error": "merchant_unavailable_after_effect"})
            return out

        @app.post("/keystone/prepare")
        async def prepare(request: Request):
            return await phase(request, "prepare")

        @app.post("/keystone/commit")
        async def commit(request: Request):
            return await phase(request, "commit")

        @app.post("/keystone/abort")
        async def abort(request: Request):
            return await phase(request, "abort")

        @app.post("/keystone/refund")
        async def refund(request: Request):
            return await phase(request, "refund")

        @app.get("/admin/state")
        async def state():
            return {"merchant_id": svc.id,
                    "offers": {k: o.available for k, o in svc.offers.items()},
                    "holds": [{"basket_id": h.basket_id, "leg_id": h.leg_id, "state": h.state, "amount": str(h.amount),
                               "currency": h.currency, "intent_id": h.intent_id} for h in svc.holds.values()]}

        @app.post("/admin/sellout")
        async def sellout(payload: dict):
            o = svc.offers[payload["offer_id"]]
            o.available = int(payload.get("left", 0))
            svc._emit("merchant.inventory", offer_id=o.offer_id, available=o.available)
            return {"ok": True}

        @app.post("/admin/sell_on_prepare")
        async def sell_on_prepare(payload: dict):
            svc.sell_on_prepare[payload["offer_id"]] = int(payload.get("left", 0))
            return {"ok": True}

        @app.post("/admin/fault")
        async def fault(payload: dict):
            svc.faults.append(Fault(payload["phase"], payload.get("mode", "error"), int(payload.get("count", 1))))
            return {"ok": True}

        @app.post("/admin/rogue")
        async def rogue(payload: dict):
            svc.rogue = bool(payload.get("on", True))
            return {"ok": True}

        @app.post("/admin/reset")
        async def reset():
            svc.reset()
            return {"ok": True}

        @app.exception_handler(HTTPException)
        async def http_exc(request: Request, exc: HTTPException):
            return JSONResponse(exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail},
                                status_code=exc.status_code)

        return app

    def reset(self) -> None:
        for k, n in self.initial.items():
            self.offers[k].available = n
        self.holds.clear()
        self.checkout_tokens.clear()
        self.aborted.clear()
        self.faults.clear()
        self.rogue = False
        self.sell_on_prepare.clear()
        self.latency_s = 0.0
