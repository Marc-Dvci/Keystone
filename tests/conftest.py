import pytest

from keystone.events import Bus
from keystone.world import build_world


@pytest.fixture
def world(tmp_path):
    bus = Bus()
    w = build_world(journal_path=str(tmp_path / "journal.sqlite"), bus=bus, escalation_timeout_s=5)
    yield w


def kinds(bus, prefix=""):
    return [e.kind for e in bus.history if e.kind.startswith(prefix)]
