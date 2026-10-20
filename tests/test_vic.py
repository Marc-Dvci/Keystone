import asyncio
from decimal import Decimal

import pytest
from fastapi import HTTPException

from keystone.canonical import digest
from keystone.events import Bus
from keystone.passkey import PasskeyStore, SoftAuthenticator
from keystone.scenarios import mandate_lisbon
from keystone.vic import VicStandIn, instruction_body
from keystone.world import build_world


@pytest.fixture
def setup(tmp_path):
    store = PasskeyStore(str(tmp_path / "pk.json"))
    auth = SoftAuthenticator()
    store.register("founder", auth.create(store.registration_options("founder")))
    bus = Bus()
    vic = VicStandIn(store, bus)
    w = build_world(journal_path=str(tmp_path / "j.sqlite"), bus=bus, vic=vic, escalation_timeout_s=5)
    return w, vic, store, auth


def approve(store, auth, m):
    return auth.get(store.assertion_options(m.digest()))


async def test_passkey_instruction_then_commit_with_confirmations(setup):
    w, vic, store, auth = setup
    m = mandate_lisbon()
    iid = vic.initiate(instruction_body(m, approve(store, auth, m)))["instructionId"]
    rep = await w.coordinator.run(m, {"kind": "webauthn"}, instruction_id=iid)
    assert rep["status"] == "COMMITTED"
    ins = vic.instructions[iid]
    assert len(ins.credentials) == 4 and len(ins.confirmations) == 4
    assert {c["status"] for c in ins.confirmations} == {"COMPLETED"}


def test_tampered_mandates_rejected(setup):
    w, vic, store, auth = setup
    m = mandate_lisbon()
    body = instruction_body(m, approve(store, auth, m))
    body["mandates"][0]["declineThreshold"]["amount"] = "99999.00"
    with pytest.raises(HTTPException):
        vic.initiate(body)


def test_assertion_for_other_mandate_rejected(setup):
    w, vic, store, auth = setup
    m = mandate_lisbon()
    other = m.model_copy(update={"cap": Decimal("20000.00")})
    body = instruction_body(other, approve(store, auth, m))
    with pytest.raises(HTTPException) as e:
        vic.initiate(body)
    assert e.value.status_code == 401


async def test_escalation_needs_passkey_over_amendment(setup):
    w, vic, store, auth = setup
    m = mandate_lisbon()
    w.merchants["taberna-do-largo"].offers["DIN-6-THU"].available = 0
    iid = vic.initiate(instruction_body(m, approve(store, auth, m)))["instructionId"]

    async def on_escalation(esc):
        async def later():
            await asyncio.sleep(0.05)
            opt = esc.options[1]  # the chef's counter, above the leg's substitute cap
            am = w.coordinator.amendment_for(esc.escalation_id, opt["offer_id"])
            # A wrong assertion (over the base mandate) is refused...
            with pytest.raises(HTTPException):
                w.coordinator.resolve(esc.escalation_id, opt["offer_id"], approve(store, auth, m))
            # ...the assertion over this option's amendment is accepted.
            a = auth.get(store.assertion_options(digest(am)))
            w.coordinator.resolve(esc.escalation_id, opt["offer_id"], a)
        asyncio.get_running_loop().create_task(later())

    w.coordinator.escalate_hook = on_escalation
    rep = await w.coordinator.run(m, {"kind": "webauthn"}, instruction_id=iid)
    assert rep["status"] == "COMMITTED", rep
    assert rep["legs"]["dinner"]["amount"] == "720.00"
    assert vic.instructions[iid].thresholds["dinner"] == Decimal("720.00")
