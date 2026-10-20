"""A local stand-in shaped on Visa Intelligent Commerce's instruction API (CyberSource ACP guide):

  POST /acp/v1/instructions                      initiate: one instruction, one mandate per leg
  PUT  /acp/v1/instructions/{id}                 update: an amendment approved with a passkey
  PUT  /acp/v1/instructions/{id}/cancel          cancel
  POST /acp/v1/instructions/{id}/credentials     per-leg credential, released only inside the mandate
  POST /acp/v1/instructions/{id}/confirmations   outcome of each leg, answered with a signed payload

VIC access is pilot-gated, so this service is Keystone's own, with field names from the published
guide. Where the guide is silent Keystone adds a ``keystone`` object: the canonical basket mandate,
its digest (the passkey challenge) and the all-or-nothing flag. Credentials are the per-leg Airwallex
virtual cards.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Awaitable, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, HTTPException

from .canonical import b64u, canonical, digest
from .clock import CLOCK
from .events import BUS, Bus
from .mandate import Mandate
from .passkey import PasskeyError, PasskeyStore

PROVIDER = "VISA_PAYMENT_PASSKEY"


def instruction_body(mandate: Mandate, assertion: dict, client_correlation_id: str | None = None) -> dict:
    until = str(int(mandate.commit_by.timestamp()))
    return {
        "clientCorrelationId": client_correlation_id or str(uuid.uuid4()),
        "consumerPrompt": mandate.purpose,
        "assuranceData": [{
            "verificationType": "CARDHOLDER", "verificationMethod": "02", "verificationResults": "01",
            "verificationTimestamp": str(int(time.time())), "authenticationContext": {"action": "AUTHENTICATE"},
            "authenticatedIdentities": {"provider": PROVIDER, "id": assertion.get("id", ""),
                                        "data": b64u(json.dumps(assertion).encode())}}],
        "mandates": [{"mandateId": l.leg_id, "description": l.title,
                      "declineThreshold": {"amount": str(l.substitution.max_price if l.substitution.allowed and
                                                         l.substitution.max_price else l.max_price),
                                           "currencyCode": l.currency},
                      "effectiveUntilTime": until, "preferredMerchantName": l.preferred_merchant or "",
                      "quantity": str(l.quantity)} for l in mandate.legs],
        "keystone": {"basketDigest": mandate.digest(), "atomic": mandate.atomic,
                     "cap": {"amount": str(mandate.cap), "currencyCode": mandate.home_currency},
                     "mandate": mandate.canonical_json()},
    }


def amendment(basket_id: str, mandate_digest: str, leg_id: str, option: dict) -> dict:
    return {"type": "keystone.amendment/1", "basket_id": basket_id, "mandate_digest": mandate_digest, "leg_id": leg_id,
            "merchant_id": option["merchant_id"], "offer_id": option["offer_id"],
            "declineThreshold": {"amount": option["amount"], "currencyCode": option["currency"]}}


@dataclass
class Instruction:
    instruction_id: str
    mandate: Mandate
    thresholds: dict[str, Decimal]
    until: int
    status: str = "ACTIVE"
    approvals: list[dict] = field(default_factory=list)
    credentials: list[dict] = field(default_factory=list)
    confirmations: list[dict] = field(default_factory=list)


CredentialSource = Callable[[str, str], Awaitable[dict]]  # (instruction_id, leg_id) -> card dict


class VicStandIn:
    def __init__(self, passkeys: PasskeyStore | None, bus: Bus = BUS, require_passkey: bool = True):
        self.passkeys = passkeys
        self.bus = bus
        self.require_passkey = require_passkey
        self.instructions: dict[str, Instruction] = {}
        self.by_basket: dict[str, str] = {}
        self.key = Ed25519PrivateKey.generate()
        self.source: CredentialSource | None = None

    def _verify(self, d: str, body: dict) -> dict:
        ids = (body.get("assuranceData") or [{}])[0].get("authenticatedIdentities") or {}
        if ids.get("provider") != PROVIDER:
            raise HTTPException(400, {"error": "assurance_missing"})
        assertion = json.loads(base64.urlsafe_b64decode(ids["data"] + "=" * (-len(ids["data"]) % 4)))
        if assertion.get("kind") == "test" and not self.require_passkey:
            return {"kind": "test", "digest": d}
        if self.passkeys is None:
            raise HTTPException(400, {"error": "no_passkey_store"})
        try:
            return self.passkeys.verify(d, assertion)
        except PasskeyError as e:
            raise HTTPException(401, {"error": "passkey_rejected", "detail": str(e)})

    def initiate(self, body: dict) -> dict:
        ks = body["keystone"]
        mandate = Mandate.model_validate(ks["mandate"])
        d = mandate.digest()
        if d != ks["basketDigest"]:
            raise HTTPException(400, {"error": "digest_mismatch"})
        # The VIC mandates must say the same as the canonical basket.
        if body["mandates"] != instruction_body(mandate, {})["mandates"]:
            raise HTTPException(400, {"error": "mandates_do_not_match_basket"})
        approval = self._verify(d, body)
        iid = f"ins_{uuid.uuid4().hex[:16]}"
        thresholds = {m["mandateId"]: Decimal(m["declineThreshold"]["amount"]) for m in body["mandates"]}
        ins = Instruction(iid, mandate, thresholds, int(body["mandates"][0]["effectiveUntilTime"]), approvals=[approval])
        self.instructions[iid] = ins
        self.bus.emit("vic.instruction", "vic", instruction_id=iid, basket_digest=d, legs=list(thresholds),
                      approval=approval.get("kind"))
        return {"clientCorrelationId": body["clientCorrelationId"], "instructionId": iid}

    def update(self, iid: str, body: dict) -> dict:
        ins = self._get(iid)
        am = body["keystone"]["amendment"]
        approval = self._verify(digest(am), body)
        ins.thresholds[am["leg_id"]] = Decimal(am["declineThreshold"]["amount"])
        ins.approvals.append({**approval, "amendment": am})
        self.bus.emit("vic.amended", "vic", instruction_id=iid, leg_id=am["leg_id"],
                      threshold=f"{am['declineThreshold']['amount']} {am['declineThreshold']['currencyCode']}")
        return {"clientCorrelationId": body.get("clientCorrelationId"), "instructionId": iid}

    def cancel(self, iid: str, body: dict | None = None) -> dict:
        ins = self._get(iid)
        ins.status = "CANCELLED"
        return {"clientCorrelationId": (body or {}).get("clientCorrelationId"), "instructionId": iid}

    async def credentials(self, iid: str, body: dict) -> dict:
        ins = self._get(iid)
        if ins.status != "ACTIVE":
            raise HTTPException(409, {"error": f"instruction_{ins.status.lower()}"})
        tx = body["transactionData"][0]
        leg = tx["clientReferenceInformation"]["code"]
        amt = tx["orderInformation"]["amountDetail"]
        if leg not in ins.thresholds:
            raise HTTPException(404, {"error": "no_mandate_for_leg"})
        spec = ins.mandate.leg(leg)
        if amt["currency"] != spec.currency:
            raise HTTPException(422, {"error": "currency_outside_mandate"})
        if Decimal(amt["totalAmount"]) > ins.thresholds[leg]:
            raise HTTPException(422, {"error": "amount_above_decline_threshold",
                                      "threshold": str(ins.thresholds[leg])})
        if time.time() + CLOCK.offset.total_seconds() > ins.until:
            raise HTTPException(422, {"error": "mandate_expired"})
        card = await self.source(iid, leg)
        rec = {"transactionId": f"vtx_{uuid.uuid4().hex[:12]}", "leg": leg, "amount": amt,
               "merchant": tx["merchantInformation"]["merchantName"]}
        ins.credentials.append(rec)
        self.bus.emit("vic.credential", "vic", instruction_id=iid, leg_id=leg, merchant=rec["merchant"],
                      amount=f"{amt['totalAmount']} {amt['currency']}", last4=card["number"][-4:])
        return {"clientCorrelationId": body["clientCorrelationId"], "transactionId": rec["transactionId"],
                "status": "COMPLETED", "keystoneCredential": {"card": card}}

    def confirmations(self, iid: str, body: dict) -> dict:
        ins = self._get(iid)
        tx = body["transactionData"][0]
        rec = {"leg": tx["clientReferenceInformation"]["code"], "type": tx.get("type", "PURCHASE"),
               "status": tx.get("keystoneOutcome", "COMPLETED"), "amount": tx["orderInformation"]["amountDetail"],
               "merchant": tx["merchantInformation"]["merchantName"], "at": CLOCK.now().isoformat()}
        ins.confirmations.append(rec)
        payload = b64u(canonical(rec))
        header = b64u(json.dumps({"alg": "EdDSA", "typ": "JWT"}, separators=(",", ":")).encode())
        sig = b64u(self.key.sign(f"{header}.{payload}".encode()))
        return {"clientCorrelationId": body["clientCorrelationId"], "transactionId": f"vtx_{uuid.uuid4().hex[:12]}",
                "status": "COMPLETED", "signedPayload": f"{header}.{payload}.{sig}"}

    def _get(self, iid: str) -> Instruction:
        if iid not in self.instructions:
            raise HTTPException(404, {"error": "instruction_not_found"})
        return self.instructions[iid]

    def router(self) -> APIRouter:
        r = APIRouter(prefix="/acp/v1")

        @r.post("/instructions")
        async def ini(body: dict):
            return self.initiate(body)

        @r.put("/instructions/{iid}")
        async def upd(iid: str, body: dict):
            return self.update(iid, body)

        @r.put("/instructions/{iid}/cancel")
        async def can(iid: str, body: dict | None = None):
            return self.cancel(iid, body)

        @r.post("/instructions/{iid}/credentials")
        async def cred(iid: str, body: dict):
            return await self.credentials(iid, body)

        @r.post("/instructions/{iid}/confirmations")
        async def conf(iid: str, body: dict):
            return self.confirmations(iid, body)

        @r.get("/instructions/{iid}")
        async def get(iid: str):
            i = self._get(iid)
            return {"instructionId": iid, "status": i.status, "thresholds": {k: str(v) for k, v in i.thresholds.items()},
                    "approvals": [{k: v for k, v in a.items() if k != "assertion"} for a in i.approvals],
                    "credentials": i.credentials, "confirmations": i.confirmations}

        return r
