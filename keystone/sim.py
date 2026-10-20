"""Basket simulation: the real coordinator, merchants and Airwallex replay, run over many baskets.

For each basket a seed draws which offers a competing buyer takes between planning and purchase
(the "race" probability per leg kind) and how the person answers an escalation. Both agents see
the same draws. Outcomes are measured from what the code did; the race probabilities, the refund
delay and the escalation answers are assumptions, written into the report next to the numbers.

    python -m keystone.sim --n 400 --seed 7 --out runs/sim
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import tempfile
import time
from decimal import Decimal

from .events import Bus
from .scenarios import mandate_hardware, mandate_lisbon, merchants_hardware, merchants_lisbon
from .sequential import SequentialAgent
from .world import build_world

ASSUMPTIONS = {
    "race_probability_per_leg": {"flight": 0.06, "hotel": 0.18, "event": 0.10, "restaurant": 0.15, "component": 0.12},
    "escalation_answer": "the person approves the cheapest option with probability 0.6, otherwise declines",
    "refund_delay_days": 9,  # US DOT: card refunds within 7 business days for cancelled flights
    "fx": "replay spread 0.25% per conversion each way, spot moves by up to 0.4% over the day",
}

SCENARIOS = {"lisbon": (merchants_lisbon, mandate_lisbon), "hardware": (merchants_hardware, mandate_hardware)}


def _install_races(world, rng: random.Random, p: dict[str, float]) -> list[str]:
    """Each offer may be taken by someone else at the moment the agent tries to buy it."""
    raced: list[str] = []
    for svc in world.merchants.values():
        doomed = {oid for oid, o in svc.offers.items() if rng.random() < p.get(o.leg_kind, 0)}
        if not doomed:
            continue
        orig = svc.prepare

        async def hook(body, v, svc=svc, doomed=doomed, orig=orig):
            if body["offer_id"] in doomed:
                svc.offers[body["offer_id"]].available = 0
                raced.append(f"{svc.id}:{body['offer_id']}")
            return await orig(body, v)

        svc.prepare = hook
    return raced


async def _one(scenario: str, seed: int, agent: str, p: dict, tmp: str) -> dict:
    mk_merchants, mk_mandate = SCENARIOS[scenario]
    rng = random.Random(seed)
    bus = Bus()
    w = build_world(journal_path=os.path.join(tmp, f"{agent}-{seed}.sqlite"), bus=bus, merchant_specs=mk_merchants(),
                    escalation_timeout_s=2)
    _install_races(w, rng, p)
    approve = rng.random() < 0.6

    async def answer(esc):
        async def later():
            await asyncio.sleep(0)
            w.coordinator.resolve(esc.escalation_id, esc.options[0]["offer_id"] if approve else None, {"kind": "sim"})
        asyncio.get_running_loop().create_task(later())

    w.coordinator.escalate_hook = answer
    mandate = mk_mandate()
    t = time.perf_counter()
    if agent == "keystone":
        rep = await w.coordinator.run(mandate, {"kind": "sim"})
    else:
        c = w.coordinator
        rep = await SequentialAgent(journal=c.journal, agent_key=c.key, merchants=c.merchants, fx=c.fx,
                                    issuing=c.cards.iss, network=c.cards.net, ledger=c.ledger, http=c.http,
                                    bus=bus).run(mandate, {})
    ms = (time.perf_counter() - t) * 1000
    kinds = [e.kind for e in bus.history]
    out = {"scenario": scenario, "seed": seed, "agent": agent, "status": rep["status"],
           "charged": Decimal(rep.get("charged_home", "0")), "stranded": Decimal(rep.get("stranded_home", "0")),
           "refunded": Decimal(rep.get("refunded_home", "0")), "fx_loss": Decimal(rep.get("fx_loss_home", "0")),
           "refunds": int(rep.get("refunds", 0)), "escalations": kinds.count("escalation.opened"),
           "substitutions": kinds.count("decision.substitute"), "voids": kinds.count("merchant.voided"),
           "void_ms": rep.get("void_ms"), "ms": ms, "captures": kinds.count("merchant.captured")}
    w.journal.close()
    await w.http.aclose()
    return out


def summarise(rows: list[dict]) -> dict:
    out: dict = {}
    for scenario in sorted({r["scenario"] for r in rows}):
        for agent in ("sequential", "keystone"):
            rs = [r for r in rows if r["scenario"] == scenario and r["agent"] == agent]
            if not rs:
                continue
            done = [r for r in rs if r["status"] in ("COMMITTED", "COMPLETED")]
            failed = [r for r in rs if r not in done]
            money_moved_on_failure = [r for r in failed if r["charged"] > 0]
            out[f"{scenario}/{agent}"] = {
                "baskets": len(rs),
                "completed": len(done),
                "completion_rate": round(len(done) / len(rs), 4),
                "failed_baskets": len(failed),
                "failed_with_money_moved": len(money_moved_on_failure),
                "stranded_usd": str(sum((r["stranded"] for r in rs), Decimal(0))),
                "refunds_needed": sum(r["refunds"] for r in rs),
                "refunded_usd": str(sum((r["refunded"] for r in rs), Decimal(0))),
                "fx_loss_on_refunds_usd": str(sum((r["fx_loss"] for r in rs), Decimal(0))),
                "usd_days_tied_up_in_refunds": str(sum((r["refunded"] for r in rs), Decimal(0))
                                                   * ASSUMPTIONS["refund_delay_days"]),
                "escalations": sum(r["escalations"] for r in rs),
                "substitutions": sum(r["substitutions"] for r in rs),
                "voids": sum(r["voids"] for r in rs),
                "void_ms_median": statistics.median([r["void_ms"] for r in rs if r["void_ms"] is not None] or [0]),
                "run_ms_median": round(statistics.median([r["ms"] for r in rs]), 1),
            }
    return out


async def run(n: int, seed: int, scenarios: list[str], scale: float = 1.0) -> dict:
    p = {k: min(1.0, v * scale) for k, v in ASSUMPTIONS["race_probability_per_leg"].items()}
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for scenario in scenarios:
            for i in range(n):
                s = seed * 100000 + i
                for agent in ("sequential", "keystone"):
                    rows.append(await _one(scenario, s, agent, p, tmp))
    return {"n_per_scenario": n, "seed": seed, "race_scale": scale, "assumptions": {**ASSUMPTIONS,
            "race_probability_per_leg": p}, "summary": summarise(rows), "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scenarios", default="lisbon,hardware")
    ap.add_argument("--sweep", action="store_true", help="also run race probability x0.5, x1, x2")
    ap.add_argument("--out", default="runs/sim")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    scales = [0.5, 1.0, 2.0] if a.sweep else [1.0]
    allres = {}
    for sc in scales:
        res = asyncio.run(run(a.n, a.seed, a.scenarios.split(","), sc))
        allres[str(sc)] = res
        with open(os.path.join(a.out, f"sim_x{sc}.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1, default=str)
        print(f"race x{sc}")
        for k, v in res["summary"].items():
            print(f"  {k:24s} done {v['completed']}/{v['baskets']}  failed-with-money {v['failed_with_money_moved']}"
                  f"  stranded ${v['stranded_usd']}  refunds {v['refunds_needed']}  fx-loss ${v['fx_loss_on_refunds_usd']}"
                  f"  escal {v['escalations']}  voids {v['voids']}")
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({k: {"summary": v["summary"], "assumptions": v["assumptions"], "n": v["n_per_scenario"]}
                   for k, v in allres.items()}, f, indent=1, default=str)


if __name__ == "__main__":
    main()
