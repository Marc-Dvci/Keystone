from decimal import Decimal

from keystone.scenarios import mandate_lisbon
from keystone.sequential import SequentialAgent

from .test_flow import sell_out_on_prepare


def seq(world):
    c = world.coordinator
    return SequentialAgent(journal=c.journal, agent_key=c.key, merchants=c.merchants, fx=c.fx, issuing=c.cards.iss,
                           network=c.cards.net, ledger=c.ledger, http=c.http, bus=c.bus)


async def test_sequential_strands_nonrefundable_money(world):
    sell_out_on_prepare(world, "lisbon-payments-summit", "PASS-3D")
    rep = await seq(world).run(mandate_lisbon(), {})
    assert rep["status"] == "FAILED_PARTIAL"
    # flights and hotel were paid before the passes failed; both are non-refundable in the scenario
    assert Decimal(rep["stranded_home"]) > 0


async def test_keystone_same_failure_charges_nothing(world):
    sell_out_on_prepare(world, "lisbon-payments-summit", "PASS-3D")
    rep = await world.coordinator.run(mandate_lisbon(), {})
    assert rep["status"] == "ABORTED" and rep["charged_home"] == "0.00"
