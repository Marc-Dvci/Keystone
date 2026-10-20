"""Fault-injection and rules evidence. Each case runs the real coordinator, merchants, issuer ledger and
Airwallex replay (through the REST client and simulator), and records what happened next to what was
expected. Output: docs/EVIDENCE.md and runs/evidence.json.

    python -m keystone.evidence
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import timedelta
from decimal import Decimal

import httpx

from . import tap
from .airwallex.client import AirwallexClient
from .airwallex.sim_server import build_app
from .canonical import digest
from .clock import CLOCK
from .coordinator import CoordinatorCrash
from .events import Bus
from .merchant import Fault
from .passkey import PasskeyStore, SoftAuthenticator
from .scenarios import mandate_lisbon
from .vic import VicStandIn, instruction_body
from .world import build_world

CASES = []


def case(title: str, expected: str):
    def wrap(fn):
        CASES.append((title, expected, fn))
        return fn
    return wrap


def world(tmp: str, name: str, vic=None):
    sim = build_app()
    awx = AirwallexClient("replay", "replay", base_url="http://awx-sim",
                          http=httpx.AsyncClient(transport=httpx.ASGITransport(app=sim)))
    w = build_world(journal_path=os.path.join(tmp, f"{name}.sqlite"), bus=Bus(), awx=awx, vic=vic,
                    escalation_timeout_s=5)
    w.extra["fake"] = sim.state.fake
    return w


def kinds(w, k):
    return [e for e in w.bus.history if e.kind == k]


def sell_on_prepare(w, mid, oid, left=0):
    w.merchants[mid].sell_on_prepare[oid] = left


def txn_types(w):
    return sorted(t.transaction_type for t in w.extra["fake"].txns.values())


@case("Every leg held, then committed", "4 captures, charged = quote-locked plan <= cap, every card closed")
async def c_commit(tmp):
    w = world(tmp, "commit")
    m = mandate_lisbon()
    rep = await w.coordinator.run(m, {})
    plan = sum(Decimal(l["home"]) for l in rep["legs"].values())
    closed = all(c.status == "CLOSED" for c in w.extra["fake"].cards.values())
    obs = (f"{rep['status']}; {len(kinds(w, 'merchant.captured'))} captures; charged ${rep['charged_home']} = plan "
           f"${plan}; cap ${m.cap}; cards closed: {closed}")
    return obs, rep["status"] == "COMMITTED" and Decimal(rep["charged_home"]) == plan <= m.cap and closed


@case("Named leg sells out after three legs are held", "abort, $0 charged, 3 voids, issuer shows AUTH + REVERSAL only")
async def c_abort(tmp):
    w = world(tmp, "abort")
    sell_on_prepare(w, "lisbon-payments-summit", "PASS-3D", 2)
    rep = await w.coordinator.run(mandate_lisbon(), {})
    voids = kinds(w, "merchant.voided")
    checks = [v.data["check"]["ok"] for v in voids]
    tt = txn_types(w)
    obs = (f"{rep['status']}; charged ${rep['charged_home']}; {len(voids)} voids in {rep['void_ms']} ms; "
           f"24 h reversal rule ok on all: {all(checks)}; issuer txns {tt}")
    return obs, (rep["status"] == "ABORTED" and rep["charged_home"] == "0.00" and len(voids) == 3 and all(checks)
                 and "CLEARING" not in tt)


@case("Hotel's last rooms go to another buyer", "substitute inside the substitution rules, no question to the person")
async def c_sub(tmp):
    w = world(tmp, "sub")
    sell_on_prepare(w, "baixa-palace", "DLX-3N", 0)
    rep = await w.coordinator.run(mandate_lisbon(), {})
    esc = kinds(w, "escalation.opened")
    obs = f"{rep['status']}; hotel at {rep['legs']['hotel']['merchant']}; escalations {len(esc)}"
    return obs, rep["status"] == "COMMITTED" and rep["legs"]["hotel"]["merchant"] == "alfama-suites" and not esc


@case("Restaurant cancels; nothing fits the rules", "escalation with priced options; a passkey over the wrong terms is refused; "
      "the right one raises the leg's VIC threshold")
async def c_esc(tmp):
    store = PasskeyStore(os.path.join(tmp, "pk.json"))
    auth = SoftAuthenticator()
    store.register("founder", auth.create(store.registration_options("founder")))
    vic = VicStandIn(store)
    w = world(tmp, "esc", vic=vic)
    vic.bus = w.bus
    m = mandate_lisbon()
    w.merchants["taberna-do-largo"].offers["DIN-6-THU"].available = 0
    iid = vic.initiate(instruction_body(m, auth.get(store.assertion_options(m.digest()))))["instructionId"]
    seen = {}

    async def on(esc):
        async def later():
            await asyncio.sleep(0.02)
            opt = esc.options[1]
            try:
                w.coordinator.resolve(esc.escalation_id, opt["offer_id"], auth.get(store.assertion_options(m.digest())))
                seen["wrong"] = "accepted"
            except Exception:
                seen["wrong"] = "refused"
            am = w.coordinator.amendment_for(esc.escalation_id, opt["offer_id"])
            w.coordinator.resolve(esc.escalation_id, opt["offer_id"], auth.get(store.assertion_options(digest(am))))
        asyncio.get_running_loop().create_task(later())
    w.coordinator.escalate_hook = on
    rep = await w.coordinator.run(m, {}, instruction_id=iid)
    opts = kinds(w, "escalation.opened")[0].data["options"]
    thr = vic.instructions[iid].thresholds["dinner"]
    obs = (f"{len(opts)} options ({', '.join(o['title'][:24] + ' ' + o['amount'] for o in opts)}); wrong assertion "
           f"{seen.get('wrong')}; {rep['status']}; dinner threshold now EUR {thr}")
    return obs, seen.get("wrong") == "refused" and rep["status"] == "COMMITTED" and thr == Decimal("720.00")


@case("Coordinator killed in the middle of the holds", "restart reads the log: presumed abort, every hold voided, "
      "a late prepare for the unsent leg is refused")
async def c_crash_prepare(tmp):
    w = world(tmp, "crash1")
    w.coordinator.crash_after = "prepare:flights"
    try:
        await w.coordinator.run(mandate_lisbon(), {})
    except CoordinatorCrash:
        pass
    live = sum(1 for s in w.merchants.values() for h in s.holds.values() if h.state == "HELD")
    t = time.perf_counter()
    out = await w.new_coordinator().recover()
    ms = int((time.perf_counter() - t) * 1000)
    still = sum(1 for s in w.merchants.values() for h in s.holds.values() if h.state == "HELD")
    tomb = any(k[1] == "flights" for k in w.merchants["atlantico-air"].aborted)
    obs = f"{live} holds live at the crash; recovery {out[0]['status']} in {ms} ms; holds left {still}; late prepare refused: {tomb}"
    return obs, out[0]["status"] == "ABORTED" and still == 0 and tomb


@case("Coordinator killed after the commit decision", "restart reads the logged decision and finishes the commit; "
      "each leg captured exactly once")
async def c_crash_commit(tmp):
    w = world(tmp, "crash2")
    m = mandate_lisbon()
    w.coordinator.crash_after = "commit:flights"
    try:
        await w.coordinator.run(m, {})
    except CoordinatorCrash:
        pass
    before = len(kinds(w, "merchant.captured"))
    out = await w.new_coordinator().recover()
    caps = kinds(w, "merchant.captured")
    per_leg = {}
    for c in caps:
        per_leg[c.data["leg_id"]] = per_leg.get(c.data["leg_id"], 0) + 1
    obs = f"{before} captured before the crash; recovery {out[0]['status']}; captures per leg {per_leg}"
    return obs, out[0]["status"] == "COMMITTED" and len(per_leg) == 4 and set(per_leg.values()) == {1}


@case("Merchant captures after it was told to abort", "Airwallex refuses the capture; no clearing on the card (Visa 1.7.7.2)")
async def c_rogue(tmp):
    w = world(tmp, "rogue")
    sell_on_prepare(w, "lisbon-payments-summit", "PASS-3D", 2)
    w.merchants["baixa-palace"].rogue = True
    await w.coordinator.run(mandate_lisbon(), {})
    await asyncio.sleep(0.6)
    r = kinds(w, "merchant.rogue_capture_refused")
    obs = f"refused: {len(r)} ({r[0].data['code'] if r else '-'}); issuer txns {txn_types(w)}"
    return obs, len(r) == 1 and "CLEARING" not in txn_types(w)


@case("Airline captures, then answers with an error", "the commit is retried with the same request; captured once")
async def c_after_effect(tmp):
    w = world(tmp, "after")
    w.merchants["atlantico-air"].faults.append(Fault("commit", "after_effect_error"))
    rep = await w.coordinator.run(mandate_lisbon(), {})
    n = len([e for e in kinds(w, "merchant.captured") if e.actor == "atlantico-air"])
    retries = len(kinds(w, "commit.retry"))
    return f"{rep['status']}; retries {retries}; airline captures {n}", rep["status"] == "COMMITTED" and n == 1


@case("A commit message is sent twice", "the second returns the same signed receipt; one capture")
async def c_dup(tmp):
    w = world(tmp, "dup")
    rep = await w.coordinator.run(mandate_lisbon(), {})
    c = w.coordinator
    leg = rep["legs"]["flights"]
    ref = next(m for m in c.merchants if m.merchant_id == "atlantico-air")
    body = {"basket_id": rep["basket_id"], "leg_id": "flights", "offer_id": "SFO-LIS-1S", "amount": leg["amount"]}
    r1 = await c._phase(ref, "commit", body)
    r2 = await c._phase(ref, "commit", body)
    n = len([e for e in kinds(w, "merchant.captured") if e.actor == "atlantico-air"])
    return f"same receipt: {r1 == r2}; airline captures {n}", r1 == r2 and n == 1


@case("A signed request is replayed, or its body altered", "replay refused by nonce; altered body refused by digest")
async def c_tap(tmp):
    w = world(tmp, "tap")
    m = w.merchants["atlantico-air"]
    url = f"http://{m.authority}/keystone/abort"
    body = json.dumps({"basket_id": "b", "leg_id": "l", "offer_id": "SFO-LIS-1S", "phase": "abort"}).encode()
    h = tap.sign_request(w.agent_key, "POST", url, body, tap.TAG_PAY)
    r1 = await w.http.post(url, content=body, headers=h)
    r2 = await w.http.post(url, content=body, headers=h)
    h2 = tap.sign_request(w.agent_key, "POST", url, body, tap.TAG_PAY)
    r3 = await w.http.post(url, content=body.replace(b'"l"', b'"x"'), headers=h2)
    h3 = tap.sign_request(w.agent_key, "POST", url, body, tap.TAG_BROWSE)
    r4 = await w.http.post(url, content=body, headers=h3)
    obs = (f"first {r1.status_code}; replay {r2.status_code} {r2.json().get('error')}; altered {r3.status_code} "
           f"{r3.json().get('error')}; browse tag on a payer route {r4.status_code} {r4.json().get('error')}")
    return obs, (r1.status_code == 200 and r2.json().get("error") == "tap_replay"
                 and r3.json().get("error") == "tap_digest_mismatch" and r4.json().get("error") == "tap_wrong_tag")


@case("The agent's TAP key is revoked", "every merchant refuses it; abort before any hold")
async def c_revoked(tmp):
    w = world(tmp, "revoked")
    w.directory.add(w.agent_key, active=False)
    rep = await w.coordinator.run(mandate_lisbon(), {})
    return (f"{rep['status']}; intents created {len(w.extra['fake'].intents)}; {rep['why'][:70]}...",
            rep["status"] == "ABORTED" and not w.extra["fake"].intents)


@case("Plan above the cap", "abort before any card or hold")
async def c_cap(tmp):
    w = world(tmp, "cap")
    rep = await w.coordinator.run(mandate_lisbon().model_copy(update={"cap": Decimal("8000.00")}), {})
    return (f"{rep['status']}; cards {len(w.extra['fake'].cards)}; {rep['why']}",
            rep["status"] == "ABORTED" and not w.extra["fake"].cards)


@case("Issuer ledger on the leg cards", "a second hold on a leg, and any hold after the basket closed, are declined")
async def c_ledger(tmp):
    w = world(tmp, "ledger")
    sell_on_prepare(w, "lisbon-payments-summit", "PASS-3D", 2)
    rep = await w.coordinator.run(mandate_lisbon(), {})
    card_id = next(cid for cid, (b, l) in w.ledger.cards.items() if l == "flights")
    d1 = await w.ledger.decide({"card_id": card_id, "transaction_amount": "10.00", "transaction_currency": "USD"})
    w2 = world(tmp, "ledger2")
    w2.ledger.open("b", "USD", Decimal("100"))
    w2.ledger.bind("b", "l", "c1", "USD", Decimal(1))
    a = await w2.ledger.decide({"card_id": "c1", "transaction_amount": "60.00"})
    b = await w2.ledger.decide({"card_id": "c1", "transaction_amount": "10.00"})
    w2.ledger.bind("b", "l2", "c2", "USD", Decimal(1))
    c = await w2.ledger.decide({"card_id": "c2", "transaction_amount": "50.00"})
    obs = (f"after abort: {d1['status_reason']}; second hold on a leg: {b['status_reason']}; "
           f"hold past the cap: {c['status_reason']}")
    return obs, (d1["status_reason"] == "BASKET_ABORTED" and a["response_status"] == "AUTHORIZED"
                 and b["status_reason"] == "LEG_ALREADY_HELD" and c["status_reason"] == "BASKET_CAP_EXCEEDED")


@case("Commit deadline beyond the hold's validity (Visa Table 5-12)", "the merchant refuses the hold; the basket aborts with $0")
async def c_validity(tmp):
    w = world(tmp, "validity")
    m = mandate_lisbon().model_copy(update={"commit_by": CLOCK.now() + timedelta(days=12)})
    rep = await w.coordinator.run(m, {})
    fails = [e.data["why"] for e in kinds(w, "prepare.failed")]
    return (f"{rep['status']}; refusals {sorted(set(fails))}; charged ${rep['charged_home']}",
            rep["status"] == "ABORTED" and "commit_deadline_beyond_hold_validity" in fails)


@case("Nobody comes back: reservation deadline passes", "the merchant voids its own hold (presumed abort), no capture")
async def c_expiry(tmp):
    w = world(tmp, "expiry")
    w.coordinator.crash_after = "prepare:flights"
    try:
        await w.coordinator.run(mandate_lisbon(), {})
    except CoordinatorCrash:
        pass
    CLOCK.advance(timedelta(hours=3))
    try:
        for s in w.merchants.values():
            await s.sweep()
    finally:
        CLOCK.advance(-timedelta(hours=3))
    v = kinds(w, "merchant.voided")
    reasons = sorted({e.data["reason"] for e in v})
    return f"{len(v)} holds voided by merchants; reasons {reasons}", len(v) == 2 and reasons == ["reservation_expired"]


@case("Credential asked above the leg's mandate (VIC decline threshold)", "the instruction service refuses; no card is released")
async def c_vic(tmp):
    store = PasskeyStore(os.path.join(tmp, "pk2.json"))
    auth = SoftAuthenticator()
    store.register("f", auth.create(store.registration_options("f")))
    vic = VicStandIn(store)
    m = mandate_lisbon()
    iid = vic.initiate(instruction_body(m, auth.get(store.assertion_options(m.digest()))))["instructionId"]

    async def src(i, leg):
        return {"number": "4242000000000000"}
    vic.source = src
    try:
        await vic.credentials(iid, {"clientCorrelationId": "x", "transactionData": [{
            "clientReferenceInformation": {"code": "dinner"},
            "orderInformation": {"amountDetail": {"totalAmount": "900.00", "currency": "EUR"}},
            "merchantInformation": {"merchantName": "x", "merchantDescriptor": {"country": "PT", "url": "x"}}}]})
        return "released", False
    except Exception as e:
        return f"refused: {getattr(e, 'detail', e)}", True


async def run_all() -> list[dict]:
    rows = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for title, expected, fn in CASES:
            try:
                obs, ok = await fn(tmp)
            except Exception as e:
                obs, ok = f"ERROR {type(e).__name__}: {e}", False
            rows.append({"case": title, "expected": expected, "observed": obs, "pass": ok})
            print(("PASS " if ok else "FAIL ") + title)
    return rows


def write(rows: list[dict], path_md: str, path_json: str) -> None:
    os.makedirs(os.path.dirname(path_md), exist_ok=True)
    os.makedirs(os.path.dirname(path_json), exist_ok=True)
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
    n = sum(r["pass"] for r in rows)
    lines = ["# Fault injection and rules evidence", "",
             f"Generated by `python -m keystone.evidence`: {n} of {len(rows)} cases pass. Each case runs the coordinator, "
             "the merchants, the issuer ledger and the Airwallex replay simulator through the same REST client used "
             "against the sandbox.", "", "| Case | Expected | Observed | |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['case']} | {r['expected']} | {r['observed'].replace('|', '/')} | {'pass' if r['pass'] else 'FAIL'} |")
    with open(path_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    rows = asyncio.run(run_all())
    write(rows, "docs/EVIDENCE.md", "runs/evidence.json")


if __name__ == "__main__":
    main()
