import httpx

from keystone import tap
from keystone.scenarios import mandate_lisbon


async def test_revoked_agent_key_aborts_before_any_hold(world):
    world.directory.add(world.agent_key, active=False)
    rep = await world.coordinator.run(mandate_lisbon(), {})
    assert rep["status"] == "ABORTED" and rep["charged_home"] == "0.00"
    assert "refused the agent" in rep["why"] and "unknown_or_revoked_key" in rep["why"]
    assert world.extra["fake"].intents == {}


async def test_registry_client_checks_agent_status_not_only_key():
    """Visa's sample registry keeps serving GET /keys/{id} after the agent is deactivated."""
    def handler(req: httpx.Request):
        if req.url.path == "/keys/k1":
            return httpx.Response(200, json={"key_id": "k1", "is_active": "true", "public_key": "x", "agent_id": 7})
        if req.url.path == "/agents/7":
            return httpx.Response(200, json={"id": 7, "is_active": "false"})
        return httpx.Response(404)
    d = tap.RegistryDirectory("http://reg", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await d.get("k1") is None
