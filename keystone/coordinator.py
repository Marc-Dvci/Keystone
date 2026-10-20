"""The Keystone coordinator: two-phase commit over card holds, across independent merchants.

  plan      choose an offer per leg inside the mandate; order legs scarcest first
  lock      take an Airwallex FX quote per foreign leg; the home-currency total must fit the cap
  issue     one Airwallex Visa virtual card per leg, locked to the leg
  prepare   TAP-signed hold request to each merchant; a hold receipt is a yes vote
  decide    on a failed leg: substitute inside the rules, escalate to the person, or abort
  commit    decision written to the log first; convert at the locked quotes; capture, most fragile first
  abort     decision written first; void every hold at once; close every card

Typed values and money live in code. The model (if configured) only ranks offers and writes the
explanation a person reads; ``fit()`` decides what the mandate allows.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable

import httpx

from . import tap
from .airwallex.base import AirwallexError, Quote
from .canonical import digest
from .clock import CLOCK
from .events import BUS, Bus
from .issuer import BasketLedger, LegCards
from .journal import Journal
from .mandate import Fit, LegSpec, Mandate, Offer, fit
from .money import quantize


@dataclass
class MerchantRef:
    merchant_id: str
    base_url: str
    kinds: list[str]


@dataclass
class LegRun:
    spec: LegSpec
    offer: Offer | None = None
    fit: Fit | None = None
    state: str = "PLANNED"  # PLANNED | HELD | FAILED | COMMITTED | VOIDED | ESCALATED
    receipt: dict | None = None
    quote: Quote | None = None
    home_amount: Decimal | None = None
    tried: list[str] = field(default_factory=list)
    merchant: MerchantRef | None = None
    history: list[dict] = field(default_factory=list)
    limit: Decimal | None = None


@dataclass
class Escalation:
    escalation_id: str
    basket_id: str
    leg_id: str
    reason: str
    options: list[dict]
    future: asyncio.Future
    explanation: str = ""

    def to_json(self) -> dict:
        return {"escalation_id": self.escalation_id, "basket_id": self.basket_id, "leg_id": self.leg_id,
                "reason": self.reason, "options": self.options, "explanation": self.explanation}


class PhaseError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(f"{status} {body}")
        self.status = status
        self.body = body


Escalator = Callable[[Escalation], Awaitable[None]]


class Coordinator:
    def __init__(self, *, journal: Journal, agent_key: tap.AgentKey, merchants: list[MerchantRef], fx, issuing,
                 network, ledger: BasketLedger, http: httpx.AsyncClient, bus: Bus = BUS,
                 directory_url: str | None = None, advisor=None, escalate: Escalator | None = None,
                 phase_timeout_s: float = 6.0, escalation_timeout_s: float = 300.0, vic=None):
        self.journal = journal
        self.key = agent_key
        self.merchants = merchants
        self.fx = fx
        self.cards = LegCards(issuing, network, ledger, bus)
        self.cards.on_issue = lambda b, l, cid, last4: journal.append(b, "card.issued", l, card_id=cid, last4=last4)
        self.ledger = ledger
        self.http = http
        self.bus = bus
        self.directory_url = directory_url
        self.advisor = advisor
        self.escalate_hook = escalate
        self.phase_timeout_s = phase_timeout_s
        self.escalation_timeout_s = escalation_timeout_s
        self.escalations: dict[str, Escalation] = {}
        self.browse_errors: dict[str, list[str]] = {}
        self.vic = vic
        self.instructions: dict[str, str] = {}  # basket_id -> VIC instruction id
        self.crash_after: str | None = None  # fault injection: "prepare:<leg_id>" stops the coordinator there

    # ------------------------------------------------------------------ transport
    def _emit(self, kind: str, **data: Any) -> None:
        self.bus.emit(kind, "coordinator", **data)

    async def _signed(self, method: str, url: str, body: dict | None, tag: str) -> httpx.Response:
        raw = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = tap.sign_request(self.key, method, url, raw, tag, directory_url=self.directory_url)
        return await self.http.request(method, url, content=raw, headers=headers, timeout=self.phase_timeout_s)

    async def _phase(self, m: MerchantRef, phase: str, body: dict) -> dict:
        body = {**body, "phase": phase}
        r = await self._signed("POST", f"{m.base_url}/keystone/{phase}", body, tap.TAG_PAY)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text}
        if r.status_code != 200:
            raise PhaseError(r.status_code, data)
        return data

    async def browse(self, kind: str) -> list[tuple[MerchantRef, Offer]]:
        out: list[tuple[MerchantRef, Offer]] = []

        self.browse_errors[kind] = []

        async def one(m: MerchantRef):
            try:
                r = await self._signed("GET", f"{m.base_url}/catalog", None, tap.TAG_BROWSE)
                if r.status_code != 200:
                    code = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get(
                        "error", f"http_{r.status_code}")
                    self.browse_errors[kind].append(f"{m.merchant_id}: {code}")
                    self._emit("browse.refused", merchant=m.merchant_id, code=code)
                    return
                for o in r.json()["offers"]:
                    off = Offer(**o)
                    if off.leg_kind == kind:
                        out.append((m, off))
            except (httpx.HTTPError, KeyError, ValueError) as e:
                self._emit("browse.failed", merchant=m.merchant_id, error=str(e)[:120])

        await asyncio.gather(*(one(m) for m in self.merchants if kind in m.kinds))
        return out

    # ------------------------------------------------------------------ planning
    def _candidates(self, leg: LegSpec, offers: list[tuple[MerchantRef, Offer]], exclude: set[str]):
        fits = []
        for m, o in offers:
            if f"{m.merchant_id}:{o.offer_id}" in exclude:
                continue
            f = fit(leg, o)
            if f.ok:
                fits.append((m, o, f))
        # exact fits first, then cheapest
        fits.sort(key=lambda t: (t[2].substitute, t[1].total(leg.quantity)))
        return fits

    @staticmethod
    def order(legs: list[LegRun]) -> list[LegRun]:
        """Scarcest first (fewest units left per unit needed); among equals, the shortest
        reservation last, so it does not lapse while the others are being held."""
        def key(l: LegRun):
            avail = l.offer.available if l.offer else 1
            return (avail / max(l.spec.quantity, 1), -int(l.offer.attrs.get("reservation_minutes", 30)) if l.offer else 0)
        return sorted(legs, key=key)

    async def _home_amount(self, leg: LegRun, mandate: Mandate, commit_window_s: float) -> Decimal:
        total = quantize(leg.offer.total(leg.spec.quantity), leg.spec.currency)
        if leg.spec.currency == mandate.home_currency:
            leg.quote = None
            return total
        validity = _validity_for(commit_window_s)
        q = await self.fx.quote(mandate.home_currency, leg.spec.currency, total, validity)
        leg.quote = q
        self._emit("fx.quote_locked", leg_id=leg.spec.leg_id, quote_id=q.quote_id, buy=f"{q.buy_amount} {q.buy_currency}",
                   sell=f"{q.sell_amount} {q.sell_currency}", client_rate=str(q.client_rate),
                   valid_to=q.valid_to.isoformat(timespec="seconds"), validity=validity)
        return q.sell_amount

    # ------------------------------------------------------------------ run
    async def run(self, mandate: Mandate, approval: dict, basket_id: str | None = None,
                  instruction_id: str | None = None) -> dict:
        basket_id = basket_id or f"bkt_{uuid.uuid4().hex[:16]}"
        md = mandate.digest()
        if self.vic is not None:
            if instruction_id is None:
                raise ValueError("a VIC instruction approved with a passkey is required")
            self.instructions[basket_id] = instruction_id
            approval = {**approval, "instruction_id": instruction_id}
        approval = {**approval, "mandate_digest": md}
        self.journal.open_basket(basket_id, mandate.canonical_json(), approval)
        self.journal.append(basket_id, "basket.opened", mandate_digest=md)
        self._emit("basket.opened", basket_id=basket_id, mandate_digest=md, cap=f"{mandate.cap} {mandate.home_currency}",
                   legs=[l.leg_id for l in mandate.legs])
        self.ledger.open(basket_id, mandate.home_currency, mandate.cap)
        t0 = time.perf_counter()
        legs = {l.leg_id: LegRun(l) for l in mandate.legs}
        catalogs = {}
        for l in mandate.legs:
            if l.kind not in catalogs:
                catalogs[l.kind] = await self.browse(l.kind)
        # plan: legs with a fitting offer first, then decide the ones without (substitute or escalate)
        window = (mandate.commit_by - CLOCK.now()).total_seconds()
        unfilled = []
        for lr in legs.values():
            cands = self._candidates(lr.spec, catalogs[lr.spec.kind], set())
            if not cands:
                unfilled.append(lr)
                continue
            lr.merchant, lr.offer, lr.fit = (await self._rank(lr.spec, cands))[0]
            lr.home_amount = await self._home_amount(lr, mandate, window)
        for lr in unfilled:
            lr.history.append({"why": f"no offer fits {lr.spec.leg_id} as written"})
            self._emit("plan.unfilled", basket_id=basket_id, leg_id=lr.spec.leg_id)
            if not await self._decide(basket_id, mandate, lr, legs, catalogs):
                return await self._abort(basket_id, mandate, legs, lr.history[-1]["why"], t0)
        total = sum((lr.home_amount for lr in legs.values()), Decimal(0))
        self._emit("basket.planned", basket_id=basket_id, total=f"{total} {mandate.home_currency}",
                   legs={k: {"merchant": v.merchant.merchant_id, "offer": v.offer.title,
                             "amount": f"{v.offer.total(v.spec.quantity)} {v.spec.currency}",
                             "home": str(v.home_amount), "substitute": v.fit.substitute} for k, v in legs.items()})
        if total > mandate.cap:
            return await self._abort(basket_id, mandate, legs, f"plan total {total} exceeds cap {mandate.cap}", t0)
        for lr in legs.values():
            if (basket_id, lr.spec.leg_id) in self.cards.by_leg:
                continue  # issued during an escalation, with the approved limit
            rate = (lr.home_amount / lr.offer.total(lr.spec.quantity)) if lr.quote else Decimal(1)
            await self.cards.issue(basket_id, mandate, lr.spec, rate)
        ordered = self.order(list(legs.values()))
        self._emit("basket.order", basket_id=basket_id, order=[l.spec.leg_id for l in ordered],
                   why="scarcest first: fewest units left per unit needed")
        # prepare
        for lr in ordered:
            ok = await self._prepare_with_recovery(basket_id, mandate, lr, legs, catalogs)
            if not ok:
                return await self._abort(basket_id, mandate, legs, lr.history[-1].get("why", "leg failed"), t0)
        return await self._commit(basket_id, mandate, legs, t0)

    async def _prepare_leg(self, basket_id: str, mandate: Mandate, lr: LegRun) -> None:
        card, details = self.cards.by_leg[(basket_id, lr.spec.leg_id)]
        amount = str(quantize(lr.offer.total(lr.spec.quantity), lr.spec.currency))
        if self.vic is not None:
            # The credential is released by the instruction service only inside this leg's mandate.
            from fastapi import HTTPException
            try:
                cr = await self.vic.credentials(self.instructions[basket_id], {
                    "clientCorrelationId": str(uuid.uuid4()),
                    "transactionData": [{"clientReferenceInformation": {"code": lr.spec.leg_id}, "type": "PURCHASE",
                                         "orderInformation": {"amountDetail": {"totalAmount": amount,
                                                                               "currency": lr.spec.currency}},
                                         "merchantInformation": {"merchantName": lr.merchant.merchant_id,
                                                                 "merchantDescriptor": {"country": "PT",
                                                                                        "url": lr.merchant.base_url}}}]})
            except HTTPException as e:
                raise PhaseError(e.status_code, e.detail if isinstance(e.detail, dict) else {"error": str(e.detail)})
            c = cr["keystoneCredential"]["card"]
            details = type(details)(c["number"], c["expiry_month"], c["expiry_year"], c["cvc"], c.get("name", ""))
        body = {"basket_id": basket_id, "leg_id": lr.spec.leg_id, "offer_id": lr.offer.offer_id,
                "quantity": lr.spec.quantity, "amount": amount,
                "currency": lr.spec.currency, "mandate_digest": mandate.digest(),
                "commit_by": mandate.commit_by.isoformat(),
                "payment": {"card": {"number": details.number, "expiry_month": details.expiry_month,
                                     "expiry_year": details.expiry_year, "cvc": details.cvc, "name": details.name}}}
        self.journal.append(basket_id, "prepare.sent", lr.spec.leg_id, merchant=lr.merchant.merchant_id,
                            base_url=lr.merchant.base_url, offer=lr.offer.offer_id, amount=body["amount"],
                            currency=lr.spec.currency)
        self._emit("prepare.sent", basket_id=basket_id, leg_id=lr.spec.leg_id, merchant=lr.merchant.merchant_id,
                   offer=lr.offer.title, amount=f"{body['amount']} {lr.spec.currency}", card_last4=details.number[-4:])
        if self.crash_after == f"prepare:{lr.spec.leg_id}":
            raise CoordinatorCrash(lr.spec.leg_id)
        receipt = await self._phase(lr.merchant, "prepare", body)
        lr.receipt = receipt
        lr.state = "HELD"
        self.journal.append(basket_id, "prepare.held", lr.spec.leg_id, merchant=lr.merchant.merchant_id,
                            receipt=receipt)
        self._emit("prepare.held", basket_id=basket_id, leg_id=lr.spec.leg_id, merchant=lr.merchant.merchant_id,
                   intent=receipt["receipt"]["intent_id"], reserved_until=receipt["receipt"].get("reserved_until"))

    async def _prepare_with_recovery(self, basket_id, mandate, lr: LegRun, legs, catalogs) -> bool:
        attempts = 0
        while True:
            attempts += 1
            try:
                await self._prepare_leg(basket_id, mandate, lr)
                return True
            except CoordinatorCrash:
                raise
            except (PhaseError, httpx.HTTPError) as e:
                why = _why(e)
                lr.state = "FAILED"
                lr.tried.append(f"{lr.merchant.merchant_id}:{lr.offer.offer_id}")
                lr.history.append({"offer": lr.offer.title, "merchant": lr.merchant.merchant_id, "why": why})
                self.journal.append(basket_id, "prepare.failed", lr.spec.leg_id, merchant=lr.merchant.merchant_id,
                                    why=why)
                self._emit("prepare.failed", basket_id=basket_id, leg_id=lr.spec.leg_id,
                           merchant=lr.merchant.merchant_id, why=why)
                # A failed or timed-out prepare may still have left a hold: tell the merchant to abort it.
                await self._abort_leg(basket_id, lr, "prepare_failed", quiet=True)
                if attempts > 4:
                    return False
            if not await self._decide(basket_id, mandate, lr, legs, catalogs):
                return False

    async def _decide(self, basket_id, mandate: Mandate, lr: LegRun, legs, catalogs) -> bool:
        """Substitute inside the rules, escalate with priced options, or give up (abort)."""
        offers = await self.browse(lr.spec.kind)
        catalogs[lr.spec.kind] = offers
        exclude = set(lr.tried)
        held_home = sum((l.home_amount for l in legs.values() if l is not lr and l.home_amount), Decimal(0))
        room = mandate.cap - held_home
        cands = self._candidates(lr.spec, offers, exclude)
        ranked = await self._rank(lr.spec, cands)
        window = (mandate.commit_by - CLOCK.now()).total_seconds()
        for m, o, f in ranked:
            lr.merchant, lr.offer, lr.fit = m, o, f
            home = await self._home_amount(lr, mandate, window)
            if home <= room:
                lr.home_amount = home
                rate = (home / o.total(lr.spec.quantity)) if lr.quote else Decimal(1)
                if (basket_id, lr.spec.leg_id) in self.cards.by_leg:
                    # One card per attempt: the card that met the failed merchant is closed.
                    await self.cards.issue(basket_id, mandate, lr.spec, rate)
                self.journal.append(basket_id, "decision.substitute", lr.spec.leg_id, merchant=m.merchant_id,
                                    offer=o.offer_id, substitute=f.substitute)
                self._emit("decision.substitute", basket_id=basket_id, leg_id=lr.spec.leg_id, merchant=m.merchant_id,
                           offer=o.title, amount=f"{o.total(lr.spec.quantity)} {o.currency}", home=str(home),
                           within="leg rules" if not f.substitute else "substitution rules")
                return True
        # Nothing inside the rules. Offer the person priced options that break a rule but fit the cap.
        options = []
        for m, o in offers:
            if f"{m.merchant_id}:{o.offer_id}" in exclude or o.available < lr.spec.quantity:
                continue
            if o.currency != lr.spec.currency or o.mcc != lr.spec.mcc:
                continue
            f = fit(lr.spec, o)
            lr_probe = LegRun(lr.spec, offer=o)
            home = await self._home_amount(lr_probe, mandate, window)
            if home > room:
                continue
            options.append({"merchant_id": m.merchant_id, "offer_id": o.offer_id, "title": o.title,
                            "amount": str(o.total(lr.spec.quantity)), "currency": o.currency, "home": str(home),
                            "breaks": f.reasons, "attrs": o.attrs})
        options.sort(key=lambda x: Decimal(x["home"]))
        options = options[:3]
        if not options:
            refused = self.browse_errors.get(lr.spec.kind) or []
            if not offers and refused:
                why = f"every merchant for {lr.spec.leg_id} refused the agent ({'; '.join(refused)})"
            else:
                why = f"no offer for {lr.spec.leg_id} fits the rules or the cap"
            lr.history.append({"why": why})
            self._emit("decision.no_option", basket_id=basket_id, leg_id=lr.spec.leg_id)
            return False
        choice = await self._escalate(basket_id, mandate, lr, options)
        if choice is None:
            lr.history.append({"why": f"person declined the options for {lr.spec.leg_id}"})
            return False
        m = next(x for x in self.merchants if x.merchant_id == choice["merchant_id"])
        o = next(o for mm, o in offers if mm.merchant_id == m.merchant_id and o.offer_id == choice["offer_id"])
        lr.merchant, lr.offer, lr.fit = m, o, Fit(ok=True, substitute=True, reasons=["approved by the person"])
        lr.home_amount = await self._home_amount(lr, mandate, window)
        rate = (lr.home_amount / o.total(lr.spec.quantity)) if lr.quote else Decimal(1)
        # The card limit follows the approval: a new card for this leg, capped at the approved amount.
        await self.cards.issue(basket_id, mandate, lr.spec, rate, limit=o.total(lr.spec.quantity))
        return True

    async def _rank(self, leg: LegSpec, cands):
        if self.advisor is None or len(cands) < 2:
            return cands
        try:
            order = await self.advisor.rank(leg, [o for _, o, _ in cands])
            by_id = {o.offer_id: (m, o, f) for m, o, f in cands}
            ranked = [by_id[i] for i in order if i in by_id]
            return ranked + [c for c in cands if c not in ranked]
        except Exception as e:  # the advisor only reorders; the rules already filtered
            self._emit("advisor.failed", error=str(e)[:160])
            return cands

    async def _escalate(self, basket_id, mandate, lr: LegRun, options) -> dict | None:
        loop = asyncio.get_running_loop()
        esc = Escalation(f"esc_{uuid.uuid4().hex[:10]}", basket_id, lr.spec.leg_id,
                         lr.history[-1]["why"] if lr.history else "leg failed", options, loop.create_future())
        if self.advisor is not None:
            try:
                esc.explanation = await self.advisor.explain(lr.spec, lr.history, options)
            except Exception as e:
                self._emit("advisor.failed", error=str(e)[:160])
        if not esc.explanation:
            esc.explanation = _plain_explanation(lr, options)
        self.escalations[esc.escalation_id] = esc
        self.journal.append(basket_id, "escalation.opened", lr.spec.leg_id, escalation=esc.to_json())
        self._emit("escalation.opened", **esc.to_json())
        lr.state = "ESCALATED"
        if self.escalate_hook:
            await self.escalate_hook(esc)
        try:
            choice = await asyncio.wait_for(esc.future, self.escalation_timeout_s)
        except asyncio.TimeoutError:
            choice = None
        self.journal.append(basket_id, "escalation.closed", lr.spec.leg_id, choice=choice)
        self._emit("escalation.closed", escalation_id=esc.escalation_id, basket_id=basket_id, leg_id=lr.spec.leg_id,
                   choice=choice)
        return choice

    def amendment_for(self, escalation_id: str, offer_id: str) -> dict:
        from .vic import amendment
        esc = self.escalations[escalation_id]
        opt = next(o for o in esc.options if o["offer_id"] == offer_id)
        md = self.journal.basket(esc.basket_id)["approval"].get("mandate_digest", "")
        return amendment(esc.basket_id, md, esc.leg_id, opt)

    def resolve(self, escalation_id: str, offer_id: str | None, approval: dict | None = None) -> None:
        """Close an escalation. Choosing an option needs a passkey assertion over that option's
        amendment digest; the instruction's threshold for the leg is raised only if it verifies."""
        esc = self.escalations[escalation_id]
        if esc.future.done():
            return
        if offer_id is None:
            esc.future.set_result(None)
            return
        opt = next(o for o in esc.options if o["offer_id"] == offer_id)
        if self.vic is not None:
            from .vic import PROVIDER
            from .canonical import b64u
            am = self.amendment_for(escalation_id, offer_id)
            self.vic.update(self.instructions[esc.basket_id], {
                "clientCorrelationId": str(uuid.uuid4()), "keystone": {"amendment": am},
                "assuranceData": [{"authenticatedIdentities": {"provider": PROVIDER, "id": (approval or {}).get("id", ""),
                                                               "data": b64u(json.dumps(approval or {}).encode())}}]})
        esc.future.set_result({**opt, "approval": approval})

    # ------------------------------------------------------------------ phase two
    async def _commit(self, basket_id, mandate: Mandate, legs: dict[str, LegRun], t0: float) -> dict:
        commit_digest = digest({"basket_id": basket_id, "mandate_digest": mandate.digest(),
                                "legs": {k: v.receipt["receipt"]["intent_id"] for k, v in legs.items()}})
        self.journal.append(basket_id, "decision.commit", commit_digest=commit_digest)
        self._emit("decision.commit", basket_id=basket_id, commit_digest=commit_digest,
                   prepared_in_ms=int((time.perf_counter() - t0) * 1000))
        # Convert at the locked quotes so each foreign leg is funded at the rate the plan used.
        for lr in legs.values():
            if lr.quote is not None:
                try:
                    conv = await self.fx.convert(lr.quote, str(uuid.uuid5(uuid.NAMESPACE_URL, f"{basket_id}/{lr.spec.leg_id}/fx")))
                    self.journal.append(basket_id, "fx.converted", lr.spec.leg_id, conversion=conv.id,
                                        rate=str(conv.client_rate), sell=str(conv.sell_amount))
                    self._emit("fx.converted", basket_id=basket_id, leg_id=lr.spec.leg_id, conversion=conv.id,
                               sell=f"{conv.sell_amount} {conv.sell_currency}", buy=f"{conv.buy_amount} {conv.buy_currency}",
                               rate=str(conv.client_rate))
                except AirwallexError as e:
                    self._emit("fx.convert_failed", basket_id=basket_id, leg_id=lr.spec.leg_id, code=e.code)
        # Most fragile first: the reservation closest to lapsing.
        order = sorted(legs.values(), key=lambda l: l.receipt["receipt"].get("reserved_until", ""))
        failures = []
        for lr in order:
            ok = await self._commit_leg(basket_id, lr, commit_digest)
            if not ok:
                failures.append(lr.spec.leg_id)
        await self.cards.close_all(basket_id)
        self.ledger.close(basket_id, "COMMITTED")
        self._confirm(basket_id, legs)
        status = "COMMITTED" if not failures else "COMMITTED_WITH_ERRORS"
        self.journal.set_status(basket_id, "COMMITTED")
        self.journal.append(basket_id, "basket.done", status=status, failures=failures)
        return self._report(basket_id, mandate, legs, status, t0, failures=failures)

    async def _commit_leg(self, basket_id, lr: LegRun, commit_digest: str, retries: int = 4) -> bool:
        body = {"basket_id": basket_id, "leg_id": lr.spec.leg_id, "offer_id": lr.receipt["receipt"]["offer_id"],
                "amount": lr.receipt["receipt"]["amount"], "commit_digest": commit_digest}
        for i in range(retries):
            try:
                r = await self._phase(lr.merchant, "commit", body)
                lr.state = "COMMITTED"
                lr.receipt = r if r.get("receipt", {}).get("type") == "keystone.capture/1" else lr.receipt
                self.journal.append(basket_id, "commit.done", lr.spec.leg_id, receipt=r)
                self._emit("commit.done", basket_id=basket_id, leg_id=lr.spec.leg_id, merchant=lr.merchant.merchant_id,
                           amount=f"{body['amount']} {lr.spec.currency}")
                return True
            except (PhaseError, httpx.HTTPError) as e:
                self._emit("commit.retry", basket_id=basket_id, leg_id=lr.spec.leg_id, attempt=i + 1, why=_why(e))
                # A merchant that answered with an error after capturing is caught by the idempotent retry.
                await asyncio.sleep(0.2 * (i + 1))
        self.journal.append(basket_id, "commit.failed", lr.spec.leg_id)
        return False

    async def _abort_leg(self, basket_id: str, lr: LegRun, reason: str, quiet: bool = False) -> None:
        t = time.perf_counter()
        try:
            r = await self._phase(lr.merchant, "abort", {"basket_id": basket_id, "leg_id": lr.spec.leg_id,
                                                         "offer_id": lr.offer.offer_id, "reason": reason})
            if lr.state == "HELD":
                lr.state = "VOIDED"
            self.journal.append(basket_id, "abort.done", lr.spec.leg_id, merchant=lr.merchant.merchant_id, receipt=r)
            if not quiet:
                self._emit("abort.done", basket_id=basket_id, leg_id=lr.spec.leg_id, merchant=lr.merchant.merchant_id,
                           ms=int((time.perf_counter() - t) * 1000))
        except (PhaseError, httpx.HTTPError) as e:
            self.journal.append(basket_id, "abort.failed", lr.spec.leg_id, why=_why(e))
            self._emit("abort.failed", basket_id=basket_id, leg_id=lr.spec.leg_id, why=_why(e))

    async def _abort(self, basket_id, mandate, legs: dict[str, LegRun], why: str, t0: float) -> dict:
        self.journal.append(basket_id, "decision.abort", why=why)
        self._emit("decision.abort", basket_id=basket_id, why=why)
        t = time.perf_counter()
        held = [lr for lr in legs.values() if lr.state == "HELD"]
        await asyncio.gather(*(self._abort_leg(basket_id, lr, "basket_aborted") for lr in held))
        void_ms = int((time.perf_counter() - t) * 1000)
        await self.cards.close_all(basket_id)
        self.ledger.close(basket_id, "ABORTED")
        self._confirm(basket_id, legs)
        self.journal.set_status(basket_id, "ABORTED")
        self.journal.append(basket_id, "basket.done", status="ABORTED", why=why, void_ms=void_ms)
        self._emit("basket.aborted", basket_id=basket_id, why=why, voids=len(held), void_ms=void_ms)
        return self._report(basket_id, mandate, legs, "ABORTED", t0, why=why, void_ms=void_ms)

    def _confirm(self, basket_id: str, legs: dict[str, LegRun]) -> None:
        """Report each leg's outcome to the instruction service (VIC confirmations)."""
        iid = self.instructions.get(basket_id)
        if self.vic is None or iid is None:
            return
        for lr in legs.values():
            if lr.receipt is None and lr.state not in ("VOIDED",):
                continue
            outcome = {"COMMITTED": "COMPLETED", "VOIDED": "CANCELLED"}.get(lr.state, lr.state)
            r = self.vic.confirmations(iid, {"clientCorrelationId": str(uuid.uuid4()), "transactionData": [{
                "clientReferenceInformation": {"code": lr.spec.leg_id}, "type": "PURCHASE", "keystoneOutcome": outcome,
                "orderInformation": {"amountDetail": {"totalAmount": lr.receipt["receipt"]["amount"] if lr.receipt else "0",
                                                      "currency": lr.spec.currency}},
                "merchantInformation": {"merchantName": lr.merchant.merchant_id if lr.merchant else "",
                                        "merchantDescriptor": {"country": "PT", "url": lr.merchant.base_url if lr.merchant else ""}}}]})
            self._emit("vic.confirmation", basket_id=basket_id, leg_id=lr.spec.leg_id, outcome=outcome,
                       signed=r["signedPayload"][:24] + "...")

    def _report(self, basket_id, mandate: Mandate, legs, status, t0, **extra) -> dict:
        lines = {}
        charged = Decimal(0)
        for k, lr in legs.items():
            amt = lr.receipt["receipt"]["amount"] if lr.receipt else None
            lines[k] = {"state": lr.state, "merchant": lr.merchant.merchant_id if lr.merchant else None,
                        "offer": lr.offer.title if lr.offer else None, "amount": amt, "currency": lr.spec.currency,
                        "home": str(lr.home_amount) if lr.home_amount is not None else None,
                        "quote": lr.quote.quote_id if lr.quote else None, "history": lr.history}
            if lr.state == "COMMITTED" and lr.home_amount is not None:
                charged += lr.home_amount
        rep = {"basket_id": basket_id, "status": status, "mandate_digest": mandate.digest(),
               "cap": str(mandate.cap), "home_currency": mandate.home_currency,
               "charged_home": str(quantize(charged, mandate.home_currency)), "legs": lines,
               "elapsed_ms": int((time.perf_counter() - t0) * 1000), **extra}
        self._emit("basket.report", **rep)
        return rep

    # ------------------------------------------------------------------ recovery
    async def recover(self) -> list[dict]:
        """Finish every basket the log left open. Presumed abort: without a logged commit decision,
        every leg that may have been prepared is aborted."""
        out = []
        for basket_id, mjson, approval in self.journal.unfinished():
            mandate = Mandate.model_validate(mjson)
            entries = self.journal.entries(basket_id)
            decision = self.journal.decision(basket_id)
            sent: dict[str, dict] = {}
            held: dict[str, dict] = {}
            for e in entries:
                if e["kind"] == "prepare.sent":
                    sent[e["leg_id"]] = e["data"]
                if e["kind"] == "prepare.held":
                    held[e["leg_id"]] = e["data"]["receipt"]
            self._emit("recovery.found", basket_id=basket_id, decision=decision or "none", prepared=list(sent))
            legs = {}
            for leg_id, d in sent.items():
                spec = mandate.leg(leg_id)
                ref = next((m for m in self.merchants if m.merchant_id == d["merchant"]),
                           MerchantRef(d["merchant"], d["base_url"], [spec.kind]))
                stub = Offer(merchant_id=ref.merchant_id, offer_id=d["offer"], leg_kind=spec.kind, title=d["offer"],
                             unit_price=Decimal(d["amount"]), currency=d["currency"], mcc=spec.mcc, available=0)
                lr = LegRun(spec, offer=stub, merchant=ref, receipt=held.get(leg_id), state="HELD")
                legs[leg_id] = lr
            t0 = time.perf_counter()
            if decision == "commit":
                cd = next(e["data"]["commit_digest"] for e in entries if e["kind"] == "decision.commit")
                for lr in legs.values():
                    await self._commit_leg(basket_id, lr, cd)
                await self._close_logged_cards(basket_id, entries)
                self.journal.set_status(basket_id, "COMMITTED")
                self.journal.append(basket_id, "basket.done", status="COMMITTED", recovered=True)
                out.append({"basket_id": basket_id, "status": "COMMITTED", "recovered": True})
            else:
                why = "coordinator restarted with no commit decision in the log (presumed abort)"
                self.journal.append(basket_id, "decision.abort", why=why)
                self._emit("decision.abort", basket_id=basket_id, why=why)
                t = time.perf_counter()
                await asyncio.gather(*(self._abort_leg(basket_id, lr, "presumed_abort") for lr in legs.values()))
                void_ms = int((time.perf_counter() - t) * 1000)
                await self._close_logged_cards(basket_id, entries)
                if basket_id in self.ledger.baskets:
                    self.ledger.close(basket_id, "ABORTED")
                self.journal.set_status(basket_id, "ABORTED")
                self.journal.append(basket_id, "basket.done", status="ABORTED", recovered=True, void_ms=void_ms)
                self._emit("basket.aborted", basket_id=basket_id, why=why, voids=len(legs), void_ms=void_ms,
                           recovered=True)
                out.append({"basket_id": basket_id, "status": "ABORTED", "recovered": True, "void_ms": void_ms,
                            "elapsed_ms": int((time.perf_counter() - t0) * 1000)})
        return out


    async def _close_logged_cards(self, basket_id: str, entries: list[dict]) -> None:
        """After a restart the card objects are gone from memory; the log still names every card."""
        for e in entries:
            if e["kind"] == "card.issued":
                try:
                    await self.cards.iss.set_card_status(e["data"]["card_id"], "CLOSED")
                    self._emit("issuer.card_closed", basket_id=basket_id, leg_id=e["leg_id"], card_id=e["data"]["card_id"],
                               recovered=True)
                except Exception as ex:
                    self._emit("recovery.card_close_failed", card_id=e["data"]["card_id"], error=str(ex)[:120])


class CoordinatorCrash(Exception):
    """Raised by fault injection to stop the coordinator mid-protocol, as a kill would."""


def _validity_for(seconds: float) -> str:
    for name, s in (("MIN_15", 900), ("MIN_30", 1800), ("HR_1", 3600), ("HR_4", 14400), ("HR_8", 28800)):
        if seconds <= s:
            return name
    return "HR_24"


def _why(e: Exception) -> str:
    if isinstance(e, PhaseError):
        b = e.body
        return b.get("reason") or b.get("error") or str(e.status)
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    return type(e).__name__


def _plain_explanation(lr: LegRun, options: list[dict]) -> str:
    last = lr.history[-1] if lr.history else {}
    head = f"{lr.spec.title}: {last.get('offer', 'the planned offer')} failed ({last.get('why', 'unavailable')}). "
    head += "Nothing left satisfies your rules for this leg. "
    parts = [f"{o['title']} at {o['amount']} {o['currency']} (breaks: {', '.join(o['breaks']) or 'none'})" for o in options]
    return head + "Options that fit the basket cap: " + "; ".join(parts) + ". Choose one, or abort the basket at no cost."
