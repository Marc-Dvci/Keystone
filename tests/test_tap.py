import base64
import json
import time

import pytest

from keystone import tap


@pytest.fixture
def setup():
    key = tap.AgentKey.from_seed("agent-k1", b"test")
    d = tap.StaticDirectory()
    d.add(key)
    return key, d, tap.MemoryNonceStore()


def _verify(headers, body, d, n, url="http://hotel.localhost:8101/keystone/prepare", method="POST", **kw):
    u = url.split("/", 3)
    return tap.verify_request(method, u[2], "/" + u[3], headers, body, d, n, **kw)


async def test_roundtrip(setup):
    key, d, n = setup
    body = json.dumps({"basket_id": "b1", "phase": "prepare"}).encode()
    h = tap.sign_request(key, "POST", "http://hotel.localhost:8101/keystone/prepare", body, tap.TAG_PAY)
    v = await _verify(h, body, d, n, require_tag=tap.TAG_PAY)
    assert v.key_id == "agent-k1" and v.tag == tap.TAG_PAY


async def test_tampered_body_rejected(setup):
    key, d, n = setup
    body = b'{"amount":"100.00"}'
    h = tap.sign_request(key, "POST", "http://hotel.localhost:8101/keystone/prepare", body, tap.TAG_PAY)
    with pytest.raises(tap.TapError) as e:
        await _verify(h, b'{"amount":"999.00"}', d, n)
    assert e.value.code == "digest_mismatch"


async def test_digest_swapped_with_body_rejected(setup):
    key, d, n = setup
    body = b'{"amount":"100.00"}'
    h = tap.sign_request(key, "POST", "http://hotel.localhost:8101/keystone/prepare", body, tap.TAG_PAY)
    evil = b'{"amount":"999.00"}'
    h["content-digest"] = tap.content_digest(evil)
    with pytest.raises(tap.TapError) as e:
        await _verify(h, evil, d, n)
    assert e.value.code == "bad_signature"


async def test_replay_rejected(setup):
    key, d, n = setup
    body = b"{}"
    h = tap.sign_request(key, "POST", "http://hotel.localhost:8101/keystone/commit", body, tap.TAG_PAY)
    await _verify(h, body, d, n, url="http://hotel.localhost:8101/keystone/commit")
    with pytest.raises(tap.TapError) as e:
        await _verify(h, body, d, n, url="http://hotel.localhost:8101/keystone/commit")
    assert e.value.code == "replay"


async def test_path_bound(setup):
    key, d, n = setup
    body = b"{}"
    h = tap.sign_request(key, "POST", "http://hotel.localhost:8101/keystone/abort", body, tap.TAG_PAY)
    with pytest.raises(tap.TapError) as e:
        await _verify(h, body, d, n, url="http://hotel.localhost:8101/keystone/commit")
    assert e.value.code == "bad_signature"


async def test_revoked_key(setup):
    key, d, n = setup
    d.add(key, active=False)
    h = tap.sign_request(key, "POST", "http://h.localhost/keystone/prepare", b"{}", tap.TAG_PAY)
    with pytest.raises(tap.TapError) as e:
        await _verify(h, b"{}", d, n, url="http://h.localhost/keystone/prepare")
    assert e.value.code == "unknown_or_revoked_key"


async def test_browse_tag_refused_on_payer_route(setup):
    key, d, n = setup
    h = tap.sign_request(key, "POST", "http://h.localhost/keystone/prepare", b"{}", tap.TAG_BROWSE)
    with pytest.raises(tap.TapError) as e:
        await _verify(h, b"{}", d, n, url="http://h.localhost/keystone/prepare", require_tag=tap.TAG_PAY)
    assert e.value.code == "wrong_tag"


async def test_expired(setup):
    key, d, n = setup
    old = int(time.time()) - 1000
    h = tap.sign_request(key, "POST", "http://h.localhost/keystone/prepare", b"{}", tap.TAG_PAY, now=old)
    with pytest.raises(tap.TapError) as e:
        await _verify(h, b"{}", d, n, url="http://h.localhost/keystone/prepare")
    assert e.value.code == "expired"


async def test_visa_sample_serialisation_accepted(setup):
    """Visa's sample agent writes '; ' separators, keyId, and covers only @authority and @path."""
    key, d, n = setup
    created = int(time.time())
    params = (f'("@authority" "@path"); created={created}; expires={created + 300}; keyId="agent-k1"; '
              f'alg="ed25519"; nonce="n-123"; tag="agent-browser-auth"')
    base = f'"@authority": shop.localhost:3001\n"@path": /product/1\n"@signature-params": {params}'
    sig = base64.b64encode(key.private.sign(base.encode())).decode()
    h = {"Signature-Input": f"sig2={params}", "Signature": f"sig2=:{sig}:"}
    v = await tap.verify_request("GET", "shop.localhost:3001", "/product/1", h, b"", d, n)
    assert v.tag == tap.TAG_BROWSE
