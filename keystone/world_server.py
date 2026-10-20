"""Process 1: the merchants and the network side.

Serves every scenario merchant on its own port (real HTTP, real TAP verification), the network
bridge between Airwallex acquiring and issuing, and, in replay mode, the Airwallex simulator on
:8090. Authorizations on leg cards are decided by the Keystone process's remote-authorization
endpoint, over HTTP, signed as Airwallex signs them. Events are forwarded to the console.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import httpx
import uvicorn

from . import tap
from .airwallex.client import SANDBOX, AirwallexClient
from .airwallex.sim_server import build_app as build_sim
from .events import Bus
from .merchant import MerchantService
from .network import Network
from .remote_auth import Caller
from .scenarios import merchants_hardware, merchants_lisbon

KEYSTONE = os.environ.get("KEYSTONE_URL", "http://127.0.0.1:8000")
SIM_PORT = int(os.environ.get("AWX_SIM_PORT", "8090"))


def live() -> bool:
    return os.environ.get("KEYSTONE_MODE", "replay") == "live" and bool(os.environ.get("AWX_CLIENT_ID"))


class Forwarder:
    """Sends world events to the console in order, without blocking the merchants."""

    def __init__(self, url: str):
        self.url = url
        self.q: asyncio.Queue = asyncio.Queue()
        self.http = httpx.AsyncClient(timeout=3)

    def __call__(self, ev) -> None:
        self.q.put_nowait(ev.to_json())

    async def pump(self) -> None:
        while True:
            batch = [await self.q.get()]
            while not self.q.empty() and len(batch) < 50:
                batch.append(self.q.get_nowait())
            try:
                await self.http.post(self.url, json=batch)
            except httpx.HTTPError:
                pass


async def main() -> None:
    bus = Bus()
    fwd = Forwarder(f"{KEYSTONE}/events/ingest")
    bus.add_sink(fwd)
    secret = os.environ.get("KEYSTONE_REMOTE_AUTH_SECRET", "replay-remote-auth-secret")
    caller = Caller(f"{KEYSTONE}/issuer/remote-auth", secret)
    servers = []
    if live():
        awx = AirwallexClient(base_url=SANDBOX)
        awx.remote_auth = caller  # relayed: remote auth is not enabled on the sandbox account
    else:
        sim = build_sim()
        servers.append(uvicorn.Server(uvicorn.Config(sim, host="127.0.0.1", port=SIM_PORT, log_level="warning")))
        sim.state.fake.remote_auth = caller  # the simulator calls remote auth the way Airwallex does
        awx = AirwallexClient("replay", "replay", base_url=f"http://127.0.0.1:{SIM_PORT}")
    network = Network(awx, awx, bus, live=live())
    directory = tap.RegistryDirectory(os.environ.get("KEYSTONE_REGISTRY_URL", f"{KEYSTONE}/registry"), ttl_s=2)
    for spec in merchants_lisbon() + merchants_hardware():
        svc = MerchantService(spec["id"], spec["name"], f"127.0.0.1:{spec['port']}", spec["mcc"], spec["currency"],
                              [o.model_copy() for o in spec["offers"]], awx, network, directory,
                              timedelta(minutes=spec["ttl_min"]), pre_auth=spec["mcc"] == 7011, bus=bus,
                              checkout="element" if live() else "server")
        servers.append(uvicorn.Server(uvicorn.Config(svc.app, host="127.0.0.1", port=spec["port"], log_level="warning")))
    print(f"world: {len(servers)} servers, mode={'live' if live() else 'replay'}", flush=True)
    await asyncio.gather(fwd.pump(), *(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
