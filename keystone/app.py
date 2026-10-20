"""Process 2: Keystone itself. Coordinator, durable log, issuer basket ledger, passkeys, the VIC-shaped
instruction service, the TAP key directory, and the console.

    python -m keystone.app        (normally started by ``python -m keystone.serve``)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import remote_auth, tap
from .airwallex.client import SANDBOX, AirwallexClient
from .canonical import digest
from .checkout import AgentBrowser
from .network import SANDBOX_TEST_CARD
from .coordinator import Coordinator, CoordinatorCrash, MerchantRef
from .events import Bus
from .issuer import BasketLedger
from .journal import Journal
from .passkey import PasskeyError, PasskeyStore, SoftAuthenticator
from .scenarios import mandate_hardware, mandate_lisbon, merchants_hardware, merchants_lisbon
from .sequential import SequentialAgent
from .advisor import from_env as advisor_from_env
from .vic import VicStandIn, instruction_body

ROOT = Path(os.environ.get("KEYSTONE_DATA", "runs/console"))
ROOT.mkdir(parents=True, exist_ok=True)
WEB = Path(__file__).parent / "web"
ORIGIN = os.environ.get("KEYSTONE_ORIGIN", "http://localhost:8000")
LIVE = os.environ.get("KEYSTONE_MODE", "replay") == "live" and bool(os.environ.get("AWX_CLIENT_ID"))
SECRET = os.environ.get("KEYSTONE_REMOTE_AUTH_SECRET", "replay-remote-auth-secret")
CRASH_EXIT = 75

SCENARIOS = {
    "lisbon": {"title": "Lisbon: customer meeting and summit", "mandate": mandate_lisbon, "merchants": merchants_lisbon},
    "hardware": {"title": "Pilot run: chips, boards, screens", "mandate": mandate_hardware, "merchants": merchants_hardware},
}

bus = Bus()
bus.load(str(ROOT / "events.jsonl"))
bus.jsonl_sink(str(ROOT / "events.jsonl"))
awx = AirwallexClient(base_url=SANDBOX) if LIVE else AirwallexClient(
    "replay", "replay", base_url=os.environ.get("AWX_SIM_URL", "http://127.0.0.1:8090"))
ledger = BasketLedger(bus)
journal = Journal(str(ROOT / "journal.sqlite"))
passkeys = PasskeyStore(str(ROOT / "passkeys.json"), rp_id="localhost", origins=[ORIGIN, "http://127.0.0.1:8000"])
demo_auth = SoftAuthenticator(origin=ORIGIN, seed=b"keystone-demo-passkey")
vic = VicStandIn(passkeys, bus)
agent_key = tap.AgentKey.from_seed("keystone-agent-1", os.environ.get("KEYSTONE_AGENT_SEED", "keystone-demo").encode())
key_active = {"v": True}
browser = AgentBrowser(agent_key, f"{ORIGIN}/registry") if LIVE else None


class _NoRoutes:
    def register_card(self, pan, card_id):  # routing happens by card number in the simulation
        pass


def _refs(scenario: str) -> list[MerchantRef]:
    return [MerchantRef(m["id"], f"http://127.0.0.1:{m['port']}", m["kinds"]) for m in SCENARIOS[scenario]["merchants"]()]


http = httpx.AsyncClient(timeout=10)
state: dict = {"scenario": "lisbon", "running": None, "last_report": None, "seq_report": None, "crash_next": None,
               "baskets": {}}
coordinators: dict[str, Coordinator] = {}


def make_coordinator(scenario: str, cls=Coordinator) -> Coordinator:
    c = cls(journal=journal, agent_key=agent_key, merchants=_refs(scenario), fx=awx, issuing=awx, network=_NoRoutes(),
            ledger=ledger, http=http, bus=bus, directory_url=f"{ORIGIN}/registry", vic=vic if cls is Coordinator else None,
            escalation_timeout_s=600, browser=browser, checkout_card=SANDBOX_TEST_CARD if LIVE else None,
            phase_timeout_s=20 if LIVE else 6, advisor=advisor_from_env(SCENARIOS[scenario]["mandate"]().purpose))

    return c


async def _vic_source(iid: str, leg_id: str) -> dict:
    """The leg card behind a VIC credential, whichever coordinator holds the basket."""
    for c in coordinators.values():
        for b, i in c.instructions.items():
            if i == iid and (b, leg_id) in c.cards.by_leg:
                _, d = c.cards.by_leg[(b, leg_id)]
                return {"number": d.number, "expiry_month": d.expiry_month, "expiry_year": d.expiry_year,
                        "cvc": d.cvc, "name": d.name}
    raise HTTPException(404, {"error": "no_card_for_leg"})


vic.source = _vic_source
recovered = asyncio.Event()
_tasks: set = set()


app = FastAPI(title="Keystone")


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse(exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}, status_code=exc.status_code)


app.include_router(vic.router())


@app.on_event("startup")
async def startup():
    bus.emit("keystone.started", "coordinator", mode="live" if LIVE else "replay", pid=os.getpid())
    # Keep a reference: the event loop holds tasks weakly, and a collected task never sets `recovered`.
    _tasks.add(asyncio.get_running_loop().create_task(_recover()))


async def _recover():
    try:
        await _recover_inner()
    finally:
        recovered.set()


async def _recover_inner():
    unfinished = journal.unfinished()
    if not unfinished:
        return
    await asyncio.sleep(1.0)  # let the merchants' process come up after a restart
    scen = "hardware" if any("pilot" in m.get("mandate_id", "") for _, m, _ in unfinished) else "lisbon"
    c = make_coordinator(scen)
    for b, _, _ in unfinished:
        coordinators[b] = c
    out = await c.recover()
    state["last_report"] = {"recovered": True, "basket_id": out[-1]["basket_id"] if out else None, "results": out}


# ---------------------------------------------------------------- TAP key directory (Visa registry shape)
@app.get("/registry/keys/{key_id}")
async def registry_key(key_id: str):
    if key_id != agent_key.key_id or not key_active["v"]:
        raise HTTPException(404, {"detail": f"Key not found for ID: {key_id}"})
    return {"key_id": key_id, "is_active": "true", "public_key": agent_key.public_b64, "algorithm": "ed25519",
            "description": "Keystone buyer agent", "agent_name": "Keystone", "agent_domain": ORIGIN}


@app.post("/api/tap/revoke")
async def tap_revoke(payload: dict):
    key_active["v"] = not bool(payload.get("revoked", True))
    bus.emit("tap.key_status", "registry", key_id=agent_key.key_id, active=key_active["v"])
    return {"active": key_active["v"]}


# ---------------------------------------------------------------- remote authorization + world events
@app.post("/issuer/remote-auth")
async def issuer_remote_auth(request: Request):
    return await remote_auth.endpoint(request, SECRET, ledger)


@app.post("/events/ingest")
async def ingest(batch: list[dict]):
    for e in batch:
        bus.emit(e["kind"], e["actor"], **e["data"])
        if e["kind"] == "issuer.reversal":
            ledger.released(e["data"].get("card_id", ""))
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request, since: int = 0):
    q = bus.subscribe()

    async def gen():
        try:
            for ev in bus.history[since:]:
                yield {"data": json.dumps(ev.to_json())}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield {"data": json.dumps(ev.to_json())}
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
        finally:
            bus.unsubscribe(q)
    return EventSourceResponse(gen())


# ---------------------------------------------------------------- passkeys
@app.get("/api/passkey/status")
async def passkey_status():
    return {"registered": len(passkeys.creds), "users": sorted({c.user for c in passkeys.creds.values()})}


@app.post("/api/passkey/register/options")
async def reg_options(payload: dict):
    return passkeys.registration_options(payload.get("user", "founder"))


@app.post("/api/passkey/register")
async def reg(payload: dict):
    try:
        c = passkeys.register(payload.get("user", "founder"), payload["credential"])
    except Exception as e:
        raise HTTPException(400, {"error": "registration_failed", "detail": str(e)})
    bus.emit("passkey.registered", "person", user=c.user, credential=c.credential_id[:12])
    return {"ok": True}


def _demo_passkey() -> None:
    if not any(c.user == "demo-passkey" for c in passkeys.creds.values()):
        passkeys.register("demo-passkey", demo_auth.create(passkeys.registration_options("demo-passkey")))


# ---------------------------------------------------------------- scenario and run
@app.get("/api/scenario")
async def scenario(name: str = "lisbon"):
    m = SCENARIOS[name]["mandate"]()
    state["scenario"] = name
    state["pending_mandate"] = m
    return {"name": name, "title": SCENARIOS[name]["title"], "mandate": m.canonical_json(), "digest": m.digest(),
            "assertion_options": passkeys.assertion_options(m.digest()) if passkeys.creds else None,
            "mode": "live" if LIVE else "replay",
            "merchants": [{"id": x["id"], "name": x["name"], "currency": x["currency"]} for x in SCENARIOS[name]["merchants"]()],
            "running": state["running"]}


@app.post("/api/run")
async def run(payload: dict):
    await recovered.wait()
    if state["running"]:
        raise HTTPException(409, {"error": "a basket is already running"})
    name = payload.get("scenario", state["scenario"])
    m = state.get("pending_mandate") or SCENARIOS[name]["mandate"]()
    if m.digest() != payload.get("digest"):
        m = SCENARIOS[name]["mandate"]()
        if m.digest() != payload.get("digest"):
            raise HTTPException(409, {"error": "mandate_changed", "detail": "reload: the terms you approved are stale"})
    assertion = payload.get("assertion")
    if payload.get("demo_passkey"):
        _demo_passkey()
        assertion = demo_auth.get(passkeys.assertion_options(m.digest()))
    try:
        ins = vic.initiate(instruction_body(m, assertion or {}))
    except HTTPException as e:
        bus.emit("passkey.rejected", "vic", detail=str(e.detail))
        raise
    await _apply_faults(name, payload.get("faults") or [])
    c = make_coordinator(name)
    if "crash" in (payload.get("faults") or []):
        c.crash_after = "prepare:flights" if name == "lisbon" else "prepare:lcd"
    basket_id = f"bkt_{os.urandom(6).hex()}"
    coordinators[basket_id] = c
    state["running"] = basket_id
    state["view_basket"] = basket_id

    async def go():
        try:
            rep = await c.run(m, {"kind": "webauthn", "demo": bool(payload.get("demo_passkey"))}, basket_id=basket_id,
                              instruction_id=ins["instructionId"])
            state["last_report"] = rep
        except CoordinatorCrash as e:
            bus.emit("coordinator.killed", "coordinator", basket_id=basket_id, at=f"prepare {e}",
                     note="process exits now; the supervisor restarts it and recovery reads the log")
            await asyncio.sleep(0.4)
            os._exit(CRASH_EXIT)
        except Exception as e:  # surfaced in the console, never swallowed
            bus.emit("coordinator.error", "coordinator", basket_id=basket_id, error=f"{type(e).__name__}: {e}"[:300])
        finally:
            state["running"] = None
    _tasks.add(asyncio.get_running_loop().create_task(go()))
    return {"basket_id": basket_id, "instruction_id": ins["instructionId"]}


@app.post("/api/run-sequential")
async def run_sequential(payload: dict):
    name = payload.get("scenario", state["scenario"])
    await _apply_faults(name, payload.get("faults") or [])
    c = make_coordinator(name, SequentialAgent)
    basket_id = f"seq_{os.urandom(6).hex()}"
    coordinators[basket_id] = c
    state["view_basket"] = basket_id

    async def go():
        try:
            state["seq_report"] = await c.run(SCENARIOS[name]["mandate"](), {}, basket_id=basket_id)
        except Exception as e:
            bus.emit("coordinator.error", "sequential", error=f"{type(e).__name__}: {e}"[:300])
        finally:
            pass
    _tasks.add(asyncio.get_running_loop().create_task(go()))
    return {"ok": True}


FAULTS = {
    "lisbon": {
        "summit_sells_out": ("lisbon-payments-summit", "sell_on_prepare", {"offer_id": "PASS-3D", "left": 2}),
        "dinner_cancels": ("taberna-do-largo", "sellout", {"offer_id": "DIN-6-THU", "left": 0}),
        "hotel_last_rooms_go": ("baixa-palace", "sell_on_prepare", {"offer_id": "DLX-3N", "left": 0}),
        "rogue_hotel": ("baixa-palace", "rogue", {"on": True}),
        "airline_commit_error": ("atlantico-air", "fault", {"phase": "commit", "mode": "after_effect_error"}),
    },
    "hardware": {
        "displays_gone": ("lumatek", "sell_on_prepare", {"offer_id": "LCD-24", "left": 0}),
        "rogue_chips": ("corvid-semi", "rogue", {"on": True}),
    },
}


async def _apply_faults(name: str, faults: list[str]) -> None:
    ports = {m["id"]: m["port"] for m in SCENARIOS[name]["merchants"]()}
    for mid, port in ports.items():
        await http.post(f"http://127.0.0.1:{port}/admin/reset")
    for f in faults:
        if f not in FAULTS.get(name, {}):
            continue
        mid, kind, body = FAULTS[name][f]
        await http.post(f"http://127.0.0.1:{ports[mid]}/admin/{kind}", json=body)
        bus.emit("fault.armed", "console", fault=f, merchant=mid)


# ---------------------------------------------------------------- escalations
@app.get("/api/escalations/{eid}/options/{offer_id}")
async def esc_option(eid: str, offer_id: str):
    c = next((c for c in coordinators.values() if eid in c.escalations), None)
    if c is None:
        raise HTTPException(404, {"error": "no_escalation"})
    am = c.amendment_for(eid, offer_id)
    return {"amendment": am, "digest": digest(am), "assertion_options": passkeys.assertion_options(digest(am))}


@app.post("/api/escalations/{eid}/resolve")
async def esc_resolve(eid: str, payload: dict):
    c = next((c for c in coordinators.values() if eid in c.escalations), None)
    if c is None:
        raise HTTPException(404, {"error": "no_escalation"})
    offer_id = payload.get("offer_id")
    assertion = payload.get("assertion")
    if offer_id and payload.get("demo_passkey"):
        _demo_passkey()
        assertion = demo_auth.get(passkeys.assertion_options(digest(c.amendment_for(eid, offer_id))))
    try:
        c.resolve(eid, offer_id, assertion)
    except (HTTPException, PasskeyError) as e:
        raise HTTPException(401, {"error": "passkey_rejected", "detail": str(getattr(e, "detail", e))})
    return {"ok": True}


# ---------------------------------------------------------------- Airwallex view
@app.get("/api/airwallex")
async def airwallex_view():
    out = {"mode": "live" if LIVE else "replay", "base_url": awx.base, "intents": [], "cards": [], "balances": {},
           "calls": len(awx.calls)}
    basket = state.get("view_basket") or (state["last_report"] or {}).get("basket_id")
    c = coordinators.get(basket) if basket else None
    if c is None:
        return out
    for e in journal.entries(basket):
        if e["kind"] == "prepare.held":
            iid = e["data"]["receipt"]["receipt"]["intent_id"]
            try:
                it = await awx.get_intent(iid)
                out["intents"].append({"leg": e["leg_id"], "merchant": e["data"]["merchant"], "id": it.id,
                                       "status": it.status, "amount": str(it.amount), "currency": it.currency,
                                       "captured": str(it.captured_amount)})
            except Exception as ex:
                out["intents"].append({"leg": e["leg_id"], "id": iid, "error": str(ex)[:80]})
    for e in journal.entries(basket):
        if e["kind"] != "card.issued":
            continue
        cid = e["data"]["card_id"]
        try:
            txns = await awx.card_transactions(cid)
        except Exception:
            txns = []
        card = next((cd for (b, l), (cd, _) in c.cards.by_leg.items() if cd.id == cid), None)
        out["cards"].append({"leg": e["leg_id"], "card_id": cid, "last4": e["data"].get("last4", ""),
                             "status": card.status if card else ("CLOSED" if journal.basket(basket)["status"] != "OPEN"
                                                                 else "ACTIVE"),
                             "txns": [{"type": t.transaction_type, "status": t.status, "amount": str(t.amount),
                                       "currency": t.currency, "reason": t.failure_reason} for t in txns]})
    try:
        out["balances"] = {k: str(v) for k, v in (await awx.balances()).items()}
    except Exception:
        pass
    return out


@app.get("/api/report")
async def report():
    return {"keystone": state["last_report"], "sequential": state["seq_report"], "running": state["running"]}


@app.get("/api/journal/{basket_id}")
async def journal_view(basket_id: str):
    return {"basket": journal.basket(basket_id), "entries": journal.entries(basket_id)}


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("KEYSTONE_PORT", "8000")), log_level="warning")
    sys.exit(0)
