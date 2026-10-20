"""Wire a whole Keystone world: Airwallex (sandbox client or in-memory), the network bridge, the
issuer ledger, merchants, a TAP key directory and the coordinator.

``in_process=True`` mounts every merchant as an ASGI app on one httpx client (tests, simulation).
Otherwise the merchants are served on their own ports by ``keystone.serve``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta

import httpx

from . import tap
from .airwallex.fake import FakeAirwallex
from .coordinator import Coordinator, MerchantRef
from .events import BUS, Bus
from .issuer import BasketLedger
from .journal import Journal
from .merchant import MerchantService
from .network import Network
from .scenarios import merchants_lisbon


@dataclass
class World:
    awx: object
    network: Network
    ledger: BasketLedger
    directory: object
    agent_key: tap.AgentKey
    merchants: dict[str, MerchantService]
    refs: list[MerchantRef]
    http: httpx.AsyncClient
    journal: Journal
    bus: Bus
    coordinator: Coordinator
    extra: dict = field(default_factory=dict)

    def new_coordinator(self, **kw) -> Coordinator:
        """A fresh coordinator on the same log, as after a process restart."""
        old = self.coordinator
        self.coordinator = Coordinator(journal=Journal(self.journal.path), agent_key=self.agent_key, merchants=self.refs,
                                       fx=self.awx, issuing=self.awx, network=self.network, ledger=self.ledger,
                                       http=self.http, bus=self.bus, vic=old.vic, directory_url=old.directory_url, **kw)
        self.coordinator.cards.by_leg = old.cards.by_leg  # card ids live at Airwallex, not in the process
        if old.vic is not None:
            attach_vic(old.vic, self.coordinator)
        return self.coordinator

    def reset(self) -> None:
        for m in self.merchants.values():
            m.reset()


def build_world(*, journal_path: str, awx=None, in_process: bool = True, host: str = "127.0.0.1",
                directory=None, directory_url: str | None = None, bus: Bus = BUS, advisor=None,
                live: bool = False, merchant_specs: list[dict] | None = None, vic=None, **coord_kw) -> World:
    awx = awx or FakeAirwallex()
    ledger = BasketLedger(bus)
    network = Network(awx, awx, bus, live=live or getattr(awx, "mode", "") == "live", ledger=ledger)
    awx.remote_auth = ledger.decide
    agent_key = tap.AgentKey.from_seed("keystone-agent-1", os.environ.get("KEYSTONE_AGENT_SEED", "keystone-demo").encode())
    if directory is None:
        directory = tap.StaticDirectory()
        directory.add(agent_key, "Keystone buyer agent")
    merchants: dict[str, MerchantService] = {}
    refs: list[MerchantRef] = []
    mounts = {}
    for spec in merchant_specs or merchants_lisbon():
        authority = f"{host}:{spec['port']}"
        svc = MerchantService(spec["id"], spec["name"], authority, spec["mcc"], spec["currency"],
                              [o.model_copy() for o in spec["offers"]], awx, network, directory,
                              timedelta(minutes=spec["ttl_min"]), pre_auth=spec["mcc"] == 7011, bus=bus)
        merchants[spec["id"]] = svc
        base = f"http://{authority}"
        refs.append(MerchantRef(spec["id"], base, spec["kinds"]))
        mounts[base] = httpx.ASGITransport(app=svc.app)
    http = httpx.AsyncClient(mounts=mounts) if in_process else httpx.AsyncClient()
    journal = Journal(journal_path)
    coord = Coordinator(journal=journal, agent_key=agent_key, merchants=refs, fx=awx, issuing=awx, network=network,
                        ledger=ledger, http=http, bus=bus, directory_url=directory_url, advisor=advisor, vic=vic,
                        **coord_kw)
    if vic is not None:
        attach_vic(vic, coord)
    return World(awx, network, ledger, directory, agent_key, merchants, refs, http, journal, bus, coord)


def attach_vic(vic, coord: Coordinator) -> None:
    async def source(iid: str, leg_id: str) -> dict:
        basket = next(b for b, i in coord.instructions.items() if i == iid)
        _, d = coord.cards.by_leg[(basket, leg_id)]
        return {"number": d.number, "expiry_month": d.expiry_month, "expiry_year": d.expiry_year, "cvc": d.cvc,
                "name": d.name}
    vic.source = source
