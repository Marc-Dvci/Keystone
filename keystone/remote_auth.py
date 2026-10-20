"""Airwallex issuing remote authorization, both ends.

``endpoint`` verifies a request the way Airwallex signs it (x-nonce, base64 HMAC-SHA256 with the shared
secret, freshness from the nonce's millisecond prefix), asks the basket ledger, and echoes
``card_transaction_event_id``. ``Caller`` sends such a request; the replay simulator uses it as Airwallex
would, and in live mode it relays the decision while remote auth is not enabled on the sandbox account.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid

import httpx
from fastapi import HTTPException, Request

MAX_AGE_MS = 60_000


def sign(secret: str, nonce: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).digest()).decode()


async def endpoint(request: Request, secret: str, ledger) -> dict:
    nonce = request.headers.get("x-nonce", "")
    sig = request.headers.get("x-signature", "")
    if not nonce or not hmac.compare_digest(sign(secret, nonce), sig):
        raise HTTPException(401, {"error": "bad_signature"})
    try:
        ts = int(nonce.split(".", 1)[0])
    except ValueError:
        raise HTTPException(401, {"error": "bad_nonce"})
    if abs(time.time() * 1000 - ts) > MAX_AGE_MS:
        raise HTTPException(401, {"error": "stale_nonce"})
    body = await request.json()
    d = await ledger.decide(body)
    return {"card_transaction_event_id": body.get("card_transaction_event_id"),
            "response_status": d["response_status"], "status_reason": d.get("status_reason") or "basket ledger approved",
            # Not part of Airwallex's contract (ignored there); lets the live relay name the card by id.
            "card_id": d.get("card_id")}


class Caller:
    def __init__(self, url: str, secret: str, timeout_s: float = 2.5):
        self.url, self.secret = url, secret
        self.http = httpx.AsyncClient(timeout=timeout_s)

    async def __call__(self, req: dict) -> dict:
        nonce = f"{int(time.time() * 1000)}.{uuid.uuid4().hex[:12]}"
        body = {"version": 2, "card_transaction_event_id": req.get("card_transaction_event_id") or str(uuid.uuid4()),
                **{k: v for k, v in req.items() if k != "card_transaction_event_id"}}
        try:
            r = await self.http.post(self.url, json=body, headers={"x-nonce": nonce, "x-signature": sign(self.secret, nonce)})
            d = r.json()
        except (httpx.HTTPError, ValueError):
            return {"response_status": "DECLINED", "status_reason": "REMOTE_AUTH_TIMEOUT"}  # default_action: DECLINED
        if r.status_code != 200 or d.get("card_transaction_event_id") != body["card_transaction_event_id"]:
            return {"response_status": "DECLINED", "status_reason": "REMOTE_AUTH_INVALID_RESPONSE"}
        return d
