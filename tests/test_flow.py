import asyncio
from decimal import Decimal

from keystone.coordinator import CoordinatorCrash
from keystone.scenarios import mandate_lisbon

from .conftest import kinds


async def test_happy_path_commits_every_leg(world):
    m = mandate_lisbon()
    rep = await world.coordinator.run(m, {"kind": "test"})
    assert rep["status"] == "COMMITTED", rep
    assert all(l["state"] == "COMMITTED" for l in rep["legs"].values())
    # The charged home-currency total equals the planned (quote-locked) total.
    planned = sum(Decimal(l["home"]) for l in rep["legs"].values())
    assert Decimal(rep["charged_home"]) == planned <= m.cap
    # Every intent captured once; every card closed.
    caps = [e for e in world.bus.history if e.kind == "merchant.captured"]
    assert len(caps) == 4
    assert all(c.status == "CLOSED" for c in world.extra['fake'].cards.values())
    # Scarcest first.
    order = next(e for e in world.bus.history if e.kind == "basket.order").data["order"]
    assert order[0] in ("hotel", "dinner")


def sell_out_on_prepare(world, merchant_id, offer_id, left=2):
    svc = world.merchants[merchant_id]
    orig = svc.prepare

    async def hook(body, v):
        svc.offers[offer_id].available = left
        return await orig(body, v)

    svc.prepare = hook


async def test_unfillable_at_plan_aborts_before_any_hold(world):
    world.merchants["lisbon-payments-summit"].offers["PASS-3D"].available = 2
    rep = await world.coordinator.run(mandate_lisbon(), {"kind": "test"})
    assert rep["status"] == "ABORTED"
    assert world.extra['fake'].intents == {} and world.extra['fake'].cards == {}


async def test_sold_out_named_leg_aborts_with_zero_charged(world):
    m = mandate_lisbon()
    sell_out_on_prepare(world, "lisbon-payments-summit", "PASS-3D")
    rep = await world.coordinator.run(m, {"kind": "test"})
    assert rep["status"] == "ABORTED"
    assert rep["charged_home"] == "0.00"
    assert kinds(world.bus, "merchant.captured") == []
    voids = [e for e in world.bus.history if e.kind == "merchant.voided"]
    assert len(voids) == 3  # hotel, dinner, flights were held, then voided
    for it in world.extra['fake'].intents.values():
        assert it.status in ("CANCELLED", "REQUIRES_PAYMENT_METHOD")
    # Issuer side shows reversals for each authorization and no clearing.
    types = [t.transaction_type for t in world.extra['fake'].txns.values()]
    assert "CLEARING" not in types and types.count("REVERSAL") == 3


async def test_escalation_then_commit(world):
    m = mandate_lisbon()
    world.merchants["taberna-do-largo"].offers["DIN-6-THU"].available = 0

    async def pick(esc):
        async def later():
            await asyncio.sleep(0.05)
            world.coordinator.resolve(esc.escalation_id, esc.options[0]["offer_id"], {"kind": "test-passkey"})
        asyncio.get_running_loop().create_task(later())

    world.coordinator.escalate_hook = pick
    rep = await world.coordinator.run(m, {"kind": "test"})
    assert rep["status"] == "COMMITTED", rep
    esc = next(e for e in world.bus.history if e.kind == "escalation.opened")
    assert len(esc.data["options"]) == 2
    assert rep["legs"]["dinner"]["merchant"] == "casa-ribeira"


async def test_substitute_inside_rules_without_asking(world):
    m = mandate_lisbon()
    world.merchants["baixa-palace"].offers["DLX-3N"].available = 2  # need 3 rooms
    rep = await world.coordinator.run(m, {"kind": "test"})
    assert rep["status"] == "COMMITTED"
    assert rep["legs"]["hotel"]["merchant"] == "alfama-suites"
    assert "escalation.opened" not in kinds(world.bus)


async def test_sold_out_between_plan_and_prepare_substitutes(world):
    m = mandate_lisbon()
    orig = world.merchants["baixa-palace"].prepare

    async def sell_then_prepare(body, v):
        world.merchants["baixa-palace"].offers["DLX-3N"].available = 0
        return await orig(body, v)

    world.merchants["baixa-palace"].prepare = sell_then_prepare
    rep = await world.coordinator.run(m, {"kind": "test"})
    assert rep["status"] == "COMMITTED"
    assert rep["legs"]["hotel"]["merchant"] == "alfama-suites"


async def test_crash_mid_prepare_recovers_by_presumed_abort(world):
    m = mandate_lisbon()
    world.coordinator.crash_after = "prepare:flights"
    try:
        await world.coordinator.run(m, {"kind": "test"})
        assert False, "expected crash"
    except CoordinatorCrash:
        pass
    held = [h for s in world.merchants.values() for h in s.holds.values() if h.state == "HELD"]
    assert len(held) == 2  # hotel and dinner were held before the crash
    fresh = world.new_coordinator()
    out = await fresh.recover()
    assert out[0]["status"] == "ABORTED"
    assert all(h.state == "VOIDED" for s in world.merchants.values() for h in s.holds.values())
    # The flights prepare was logged but never sent: its merchant must refuse it if it arrives late.
    assert any(k[1] == "flights" for k in world.merchants["atlantico-air"].aborted)


async def test_rogue_merchant_capture_after_abort_is_refused(world):
    m = mandate_lisbon()
    sell_out_on_prepare(world, "lisbon-payments-summit", "PASS-3D")
    world.merchants["baixa-palace"].rogue = True
    rep = await world.coordinator.run(m, {"kind": "test"})
    assert rep["status"] == "ABORTED"
    await asyncio.sleep(0.5)
    refused = [e for e in world.bus.history if e.kind == "merchant.rogue_capture_refused"]
    assert len(refused) == 1
    assert "CLEARING" not in [t.transaction_type for t in world.extra['fake'].txns.values()]


async def test_commit_error_after_capture_is_retried_idempotently(world):
    m = mandate_lisbon()
    world.merchants["atlantico-air"].faults.append(__import__("keystone.merchant", fromlist=["Fault"]).Fault(
        "commit", "after_effect_error"))
    rep = await world.coordinator.run(m, {"kind": "test"})
    assert rep["status"] == "COMMITTED"
    caps = [e for e in world.bus.history if e.kind == "merchant.captured" and e.actor == "atlantico-air"]
    assert len(caps) == 1  # captured once despite the error and the retry


async def test_cap_enforced_by_issuer_ledger(world):
    m = mandate_lisbon().model_copy(update={"cap": Decimal("8000.00")})
    rep = await world.coordinator.run(m, {"kind": "test"})
    assert rep["status"] == "ABORTED"
    assert "exceeds cap" in rep["why"]
