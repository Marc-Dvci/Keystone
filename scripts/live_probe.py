"""Probe the Airwallex sandbox for every behaviour Keystone relies on, and write what it answered.

    set AWX_CLIENT_ID=...  set AWX_API_KEY=...
    python scripts/live_probe.py            -> docs/LIVE_SANDBOX.md and runs/live_probe.json

Uses only sandbox endpoints and test data: the documented test card 4035501000000008, synthetic cardholder
details, small amounts. Nothing here touches real money.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keystone.airwallex.base import AirwallexError, CardDetails, Controls  # noqa: E402
from keystone.airwallex.client import SANDBOX, AirwallexClient  # noqa: E402

TEST_CARD = CardDetails("4035501000000008", "12", "2030", "123", "Keystone Probe")
rows: list[dict] = []


def rid() -> str:
    return str(uuid.uuid4())


async def step(name: str, fn, expect: str):
    t = time.perf_counter()
    try:
        out = await fn()
        ok, detail = out if isinstance(out, tuple) else (True, out)
    except AirwallexError as e:
        ok, detail = False, f"{e.status} {e.code}: {e.message}"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e} {traceback.format_exc(limit=1)}"
    rows.append({"step": name, "expected": expect, "ok": ok, "observed": str(detail)[:400],
                 "ms": int((time.perf_counter() - t) * 1000)})
    print(("OK   " if ok else "FAIL ") + name + " :: " + str(detail)[:160], flush=True)
    return ok


async def main() -> None:
    if not os.environ.get("AWX_CLIENT_ID"):
        sys.exit("set AWX_CLIENT_ID and AWX_API_KEY (sandbox.airwallex.com > Account > Developer > API keys)")
    c = AirwallexClient(base_url=SANDBOX)
    s: dict = {}

    await step("login", lambda: c._auth(), "a bearer token")
    await step("balances", lambda: c.balances(), "a balance per currency")

    async def hold(currency="USD", amount=Decimal("123.45"), pre_auth=False):
        it = await c.create_intent(amount, currency, f"probe-{uuid.uuid4().hex[:8]}", rid(), {"probe": "keystone"})
        return await c.confirm_hold(it.id, TEST_CARD, rid(), pre_auth)

    async def h1():
        s["a"] = await hold()
        return s["a"].status == "REQUIRES_CAPTURE", f"{s['a'].id} {s['a'].status}"
    await step("hold (auto_capture false)", h1, "REQUIRES_CAPTURE")

    async def h2():
        s["b"] = await hold("EUR", Decimal("210.00"), pre_auth=True)
        return s["b"].status == "REQUIRES_CAPTURE", f"{s['b'].id} {s['b'].status}"
    await step("hold with pre_auth (EUR)", h2, "REQUIRES_CAPTURE")

    async def cap():
        it = await c.capture(s["a"].id, Decimal("123.45"), rid())
        return it.status == "SUCCEEDED", f"{it.status} captured {it.captured_amount}"
    await step("capture the hold", cap, "SUCCEEDED")

    async def cap2():
        try:
            await c.capture(s["a"].id, Decimal("1.00"), rid())
            return False, "second capture accepted"
        except AirwallexError as e:
            return True, f"refused {e.code}"
    await step("second capture on the same intent", cap2, "refused")

    async def void():
        it = await c.cancel(s["b"].id, "keystone basket aborted", rid())
        return it.status == "CANCELLED", f"{it.status}"
    await step("void (cancel) a held intent", void, "CANCELLED")

    async def cap_after_void():
        try:
            await c.capture(s["b"].id, Decimal("210.00"), rid())
            return False, "capture after void accepted"
        except AirwallexError as e:
            return True, f"refused {e.code}: {e.message}"
    await step("capture after void (Visa 1.7.7.2)", cap_after_void, "refused")

    async def over():
        it = await hold(amount=Decimal("50.00"))
        try:
            await c.capture(it.id, Decimal("60.00"), rid())
            return False, "capture above the hold accepted"
        except AirwallexError as e:
            await c.cancel(it.id, "probe", rid())
            return True, f"refused {e.code}"
    await step("capture above the authorized amount", over, "refused")

    async def partial():
        it = await hold(amount=Decimal("80.00"))
        it = await c.capture(it.id, Decimal("55.00"), rid())
        return it.status == "SUCCEEDED" and it.captured_amount == Decimal("55.00"), f"{it.status} {it.captured_amount}"
    await step("partial capture", partial, "SUCCEEDED, captured 55")

    async def quote():
        s["q"] = await c.quote("USD", "EUR", Decimal("1890.00"), "HR_24")
        q = s["q"]
        return True, f"{q.quote_id} client_rate {q.client_rate} sell {q.sell_amount} valid_to {q.valid_to}"
    await step("FX quote USD->EUR, HR_24", quote, "quote_id and valid_to ~24 h out")

    async def conv():
        cv = await c.convert(s["q"], rid())
        return cv.client_rate == s["q"].client_rate, f"{cv.id} {cv.status} rate {cv.client_rate}"
    await step("convert at the locked quote", conv, "same client_rate")

    async def holder():
        s["ch"] = await c.ensure_cardholder()
        return True, s["ch"]
    await step("cardholder", holder, "READY")

    async def card():
        ctl = Controls(Decimal("2000.00"), "EUR", [7011], datetime.now(timezone.utc) - timedelta(minutes=1),
                       datetime.now(timezone.utc) + timedelta(hours=2), Decimal("2000.00"))
        s["card"] = await c.create_card(s["ch"], ctl, "probe-hotel", rid())
        s["det"] = await c.card_details(s["card"].id)
        return s["card"].status == "ACTIVE", f"{s['card'].id} {s['card'].status} •{s['det'].number[-4:]}"
    await step("leg card: SINGLE, EUR, MCC 7011, limit 2000, 2 h window", card, "ACTIVE")

    async def auth_ok():
        t = await c.simulate_authorization(s["card"].id, s["det"].number, Decimal("1890.00"), "EUR", 7011, "Probe Hotel")
        s["t"] = t
        return t.status != "FAILED", f"{t.id} {t.status} {t.failure_reason}"
    await step("authorization inside the controls", auth_ok, "approved")

    async def rev():
        t = await c.simulate_reverse(s["t"].id, Decimal("1890.00"))
        return True, f"{t.transaction_type} {t.status}"
    await step("reverse it", rev, "reversal")

    async def auth_mcc():
        ctl = Controls(Decimal("100.00"), "EUR", [5812], None, None, None)
        cd = await c.create_card(s["ch"], ctl, "probe-dinner", rid())
        d = await c.card_details(cd.id)
        t = await c.simulate_authorization(cd.id, d.number, Decimal("50.00"), "EUR", 7011, "Wrong Category")
        t2 = await c.simulate_authorization(cd.id, d.number, Decimal("150.00"), "EUR", 5812, "Too Much")
        await c.set_card_status(cd.id, "CLOSED")
        return (t.failure_reason == "MERCHANT_CATEGORY_NOT_ALLOWED" and t2.failure_reason == "LIMIT_EXCEEDED",
                f"wrong MCC -> {t.failure_reason}; over limit -> {t2.failure_reason}")
    await step("controls decline: wrong MCC, over limit", auth_mcc, "MERCHANT_CATEGORY_NOT_ALLOWED, LIMIT_EXCEEDED")

    async def single():
        t = await c.simulate_authorization(s["card"].id, s["det"].number, Decimal("10.00"), "EUR", 7011, "Second Use")
        return True, f"second authorization on a SINGLE card after a reversal: {t.status} {t.failure_reason}"
    await step("SINGLE card after a reversal", single, "recorded (decides whether Keystone needs a card per attempt)")

    async def closed_loop():
        it = await c.create_intent(Decimal("10.00"), "EUR", f"probe-loop-{uuid.uuid4().hex[:6]}", rid(), {})
        try:
            it = await c.confirm_hold(it.id, s["det"], rid(), False)
            return True, f"an Airwallex-issued card paid an Airwallex intent: {it.status}"
        except AirwallexError as e:
            return True, f"not routed in sandbox ({e.code}); Keystone's network bridge mirrors it instead"
    await step("closed loop: issued card pays a payment intent", closed_loop, "recorded")

    async def close():
        await c.set_card_status(s["card"].id, "CLOSED")
        try:
            t = await c.simulate_authorization(s["card"].id, s["det"].number, Decimal("5.00"), "EUR", 7011, "After Close")
            return t.failure_reason in ("CARD_CLOSED", "CARD_INACTIVE"), f"after close: {t.failure_reason}"
        except AirwallexError as e:
            return True, f"after close: {e.code}"
    await step("close the card, then try it", close, "CARD_CLOSED")

    out = ROOT / "runs" / "live_probe.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "rows": rows, "calls": c.calls}, indent=1))
    md = ["# Airwallex sandbox probe", "", f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC against {SANDBOX}, "
          f"{len(c.calls)} API calls, {sum(r['ok'] for r in rows)} of {len(rows)} steps as expected.", "",
          "| Step | Expected | Observed | |", "|---|---|---|---|"]
    md += [f"| {r['step']} | {r['expected']} | {r['observed'].replace('|', '/')} | {'ok' if r['ok'] else 'differs'} |"
           for r in rows]
    (ROOT / "docs" / "LIVE_SANDBOX.md").write_text("\n".join(md) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
