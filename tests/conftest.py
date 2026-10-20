import httpx
import pytest

from keystone.airwallex.client import AirwallexClient
from keystone.airwallex.sim_server import build_app
from keystone.events import Bus
from keystone.world import build_world


@pytest.fixture(params=["fake", "rest"])
def world(request, tmp_path):
    """Every flow test runs twice: against the in-memory fake, and through the REST client against
    the replay simulator (same paths and JSON shapes as the Airwallex API)."""
    bus = Bus()
    awx = None
    if request.param == "rest":
        sim = build_app()
        awx = AirwallexClient("replay-client", "replay-key", base_url="http://awx-sim",
                              http=httpx.AsyncClient(transport=httpx.ASGITransport(app=sim)))
        awx._sim = sim.state.fake
    w = build_world(journal_path=str(tmp_path / "journal.sqlite"), bus=bus, escalation_timeout_s=5, awx=awx)
    w.extra["fake"] = awx._sim if awx is not None else w.awx
    yield w


def kinds(bus, prefix=""):
    return [e.kind for e in bus.history if e.kind.startswith(prefix)]
