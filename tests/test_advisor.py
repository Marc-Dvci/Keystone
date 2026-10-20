import asyncio

from keystone.scenarios import mandate_lisbon



class Reverse:
    def __init__(self):
        self.ranked = 0

    async def rank(self, leg, offers):
        self.ranked += 1
        return [o.offer_id for o in reversed(offers)] + ["INJECTED-ID"]

    async def explain(self, leg, history, options):
        return "model explanation"


class Broken:
    async def rank(self, leg, offers):
        raise RuntimeError("model down")

    async def explain(self, leg, history, options):
        raise RuntimeError("model down")


async def test_advisor_only_reorders_allowed_offers(world):
    world.coordinator.advisor = Reverse()
    rep = await world.coordinator.run(mandate_lisbon(), {})
    assert rep["status"] == "COMMITTED"
    assert world.coordinator.advisor.ranked >= 1
    # Two flight offers fit; the advisor's order wins among them, its unknown id is dropped.
    assert rep["legs"]["flights"]["merchant"] == "meridian-airways"
    assert all("INJECTED" not in str(l["offer"]) for l in rep["legs"].values())


async def test_broken_advisor_falls_back_to_rules(world):
    world.coordinator.advisor = Broken()
    world.merchants["taberna-do-largo"].offers["DIN-6-THU"].available = 0

    async def pick(esc):
        assert esc.explanation  # the rule-written explanation is used
        asyncio.get_running_loop().call_later(0.01, world.coordinator.resolve, esc.escalation_id, None)
    world.coordinator.escalate_hook = pick
    rep = await world.coordinator.run(mandate_lisbon(), {})
    assert rep["status"] == "ABORTED" and rep["charged_home"] == "0.00"


async def test_advisor_cannot_put_a_substitute_before_an_exact_fit(world):
    class PreferSubstitute:
        async def rank(self, leg, offers):
            return sorted((o.offer_id for o in offers), key=lambda i: i != "STD-3N")

        async def explain(self, leg, history, options):
            return ""
    world.coordinator.advisor = PreferSubstitute()
    rep = await world.coordinator.run(mandate_lisbon(), {})
    assert rep["legs"]["hotel"]["merchant"] == "baixa-palace"
