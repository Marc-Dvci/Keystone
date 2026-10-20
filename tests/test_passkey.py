import pytest

from keystone.passkey import PasskeyError, PasskeyStore, SoftAuthenticator
from keystone.scenarios import mandate_lisbon


def test_passkey_binds_approval_to_mandate_digest(tmp_path):
    store = PasskeyStore(str(tmp_path / "pk.json"))
    auth = SoftAuthenticator()
    store.register("founder", auth.create(store.registration_options("founder")))
    m = mandate_lisbon()
    d = m.digest()
    rec = store.verify(d, auth.get(store.assertion_options(d)))
    assert rec["digest"] == d and rec["user"] == "founder"
    # The same assertion does not approve a mandate with a higher cap.
    m2 = m.model_copy(update={"cap": m.cap + 1})
    with pytest.raises(PasskeyError):
        store.verify(m2.digest(), auth.get(store.assertion_options(d)))
