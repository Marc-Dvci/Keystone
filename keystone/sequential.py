"""Baseline: the way an agent buys a multi-merchant task today, one checkout at a time.

Same merchants, same mandate, same rules for choosing offers and substitutes. The difference is the
protocol: each leg is paid (held and captured at once) before the next is looked at. When a later
leg cannot be filled, the agent asks every paid merchant for a refund. A non-refundable leg stays
paid; a refunded foreign leg comes back in the foreign currency and is converted home at the rate of
that day.
"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal

import httpx

from .coordinator import Coordinator, LegRun, PhaseError, _why
from .mandate import Mandate
from .money import quantize


class SequentialAgent(Coordinator):
    async def run(self, mandate: Mandate, approval: dict, basket_id: str | None = None, **_) -> dict:
        basket_id = basket_id or f"seq_{uuid.uuid4().hex[:16]}"
        t0 = time.perf_counter()
        self.ledger.open(basket_id, mandate.home_currency, mandate.cap)
        self._emit("seq.started", basket_id=basket_id)
        paid: list[LegRun] = []
        legs = {l.leg_id: LegRun(l) for l in mandate.legs}
        spent_home = Decimal(0)
        failed_leg = None
        for lr in legs.values():  # the order the person wrote them
            ok = False
            exclude: set[str] = set()
            for _ in range(4):
                offers = await self.browse(lr.spec.kind)
                cands = self._candidates(lr.spec, offers, exclude)
                if not cands:
                    break
                lr.merchant, lr.offer, lr.fit = cands[0]
                lr.home_amount = await self._home_amount(lr, mandate, 60)
                if spent_home + lr.home_amount > mandate.cap:
                    exclude.add(f"{lr.merchant.merchant_id}:{lr.offer.offer_id}")
                    continue
                rate = (lr.home_amount / lr.offer.total(lr.spec.quantity)) if lr.quote else Decimal(1)
                await self.cards.issue(basket_id, mandate, lr.spec, rate)
                try:
                    await self._prepare_leg(basket_id, mandate, lr)
                    if lr.quote is not None:
                        await self.fx.convert(lr.quote, str(uuid.uuid4()))
                    await self._phase(lr.merchant, "commit", {
                        "basket_id": basket_id, "leg_id": lr.spec.leg_id, "offer_id": lr.offer.offer_id,
                        "amount": lr.receipt["receipt"]["amount"]})
                    lr.state = "COMMITTED"
                    spent_home += lr.home_amount
                    paid.append(lr)
                    self._emit("seq.paid", basket_id=basket_id, leg_id=lr.spec.leg_id, merchant=lr.merchant.merchant_id,
                               amount=f"{lr.receipt['receipt']['amount']} {lr.spec.currency}")
                    ok = True
                    break
                except (PhaseError, httpx.HTTPError) as e:
                    exclude.add(f"{lr.merchant.merchant_id}:{lr.offer.offer_id}")
                    lr.history.append({"offer": lr.offer.title, "why": _why(e)})
                    if lr.state == "HELD":
                        await self._abort_leg(basket_id, lr, "prepare_failed", quiet=True)
            if not ok:
                failed_leg = lr.spec.leg_id
                break
        await self.cards.close_all(basket_id)
        if failed_leg is None:
            self._emit("seq.unwound", basket_id=basket_id, status="COMPLETED", charged_home=str(quantize(spent_home, "USD")))
            return {"basket_id": basket_id, "status": "COMPLETED", "charged_home": str(quantize(spent_home, "USD")),
                    "stranded_home": "0.00", "refunded_home": "0.00", "fx_loss_home": "0.00", "refunds": 0,
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
        # Unwind: ask for refunds.
        stranded = Decimal(0)
        refunded = Decimal(0)
        fx_loss = Decimal(0)
        refunds = 0
        for lr in paid:
            try:
                await self._phase(lr.merchant, "refund", {"basket_id": basket_id, "leg_id": lr.spec.leg_id,
                                                          "offer_id": lr.offer.offer_id, "reason": "basket_incomplete"})
                refunds += 1
                if lr.quote is not None:
                    # Money comes back in the leg currency and is converted home at today's rate.
                    amt = Decimal(lr.receipt["receipt"]["amount"])
                    back = await self.fx.quote(lr.spec.currency, mandate.home_currency, None, "MIN_1", sell_amount=amt)
                    conv = await self.fx.convert(back, str(uuid.uuid4()))
                    refunded += conv.buy_amount
                    fx_loss += lr.home_amount - conv.buy_amount
                else:
                    refunded += lr.home_amount
            except (PhaseError, httpx.HTTPError):
                stranded += lr.home_amount
        rep = {"basket_id": basket_id, "status": "FAILED_PARTIAL" if paid else "FAILED", "failed_leg": failed_leg,
               "charged_home": str(quantize(spent_home, "USD")), "stranded_home": str(quantize(stranded, "USD")),
               "refunded_home": str(quantize(refunded, "USD")), "fx_loss_home": str(quantize(fx_loss, "USD")),
               "refunds": refunds, "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
        self._emit("seq.unwound", **rep)
        return rep
