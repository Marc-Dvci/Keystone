"""Passkey approvals (WebAuthn), bound to the exact terms approved.

The challenge of every assertion is the SHA-256 digest of what is being approved: the basket mandate,
or an amendment raising one leg after an escalation. A changed term gives a different digest, and the
old assertion no longer verifies. Visa's published Intelligent Commerce shape carries the passkey
result next to the mandates; Keystone makes the link explicit through the challenge.

``SoftAuthenticator`` produces real WebAuthn credentials (ES256, "none" attestation) for tests and
headless replays. In the console, the browser's own passkey (Windows Hello, Touch ID, a phone) is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import struct
import threading
import time
from dataclasses import dataclass

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from .canonical import b64u, b64u_decode


def challenge_for(digest: str) -> bytes:
    """'sha256:<hex>' -> the 32 bytes used as the WebAuthn challenge."""
    return bytes.fromhex(digest.split(":", 1)[1])


@dataclass
class StoredCredential:
    credential_id: str
    public_key: str
    sign_count: int
    user: str


class PasskeyStore:
    def __init__(self, path: str, rp_id: str = "localhost", rp_name: str = "Keystone",
                 origins: list[str] | None = None):
        self.path = path
        self.rp_id = rp_id
        self.rp_name = rp_name
        self.origins = origins or ["http://localhost:8000"]
        self._lock = threading.Lock()
        self._pending: dict[str, bytes] = {}
        self.creds: dict[str, StoredCredential] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for c in json.load(f):
                    self.creds[c["credential_id"]] = StoredCredential(**c)

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([c.__dict__ for c in self.creds.values()], f, indent=1)

    def registration_options(self, user: str) -> dict:
        ch = secrets.token_bytes(32)
        opts = generate_registration_options(
            rp_id=self.rp_id, rp_name=self.rp_name, user_name=user, user_display_name=user,
            user_id=hashlib.sha256(user.encode()).digest()[:16], challenge=ch,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.PREFERRED))
        self._pending["reg:" + user] = ch
        return json.loads(options_to_json(opts))

    def register(self, user: str, credential: dict, origin: str | None = None) -> StoredCredential:
        ch = self._pending.pop("reg:" + user)
        v = verify_registration_response(credential=credential, expected_challenge=ch, expected_rp_id=self.rp_id,
                                         expected_origin=origin or self.origins)
        c = StoredCredential(b64u(v.credential_id), b64u(v.credential_public_key), v.sign_count, user)
        with self._lock:
            self.creds[c.credential_id] = c
            self._save()
        return c

    def assertion_options(self, digest: str) -> dict:
        opts = generate_authentication_options(
            rp_id=self.rp_id, challenge=challenge_for(digest),
            allow_credentials=[PublicKeyCredentialDescriptor(id=b64u_decode(c.credential_id)) for c in self.creds.values()],
            user_verification=UserVerificationRequirement.PREFERRED)
        return json.loads(options_to_json(opts))

    def verify(self, digest: str, credential: dict, origin: str | None = None) -> dict:
        """Verify an assertion over ``digest``. Returns the approval record kept with the basket."""
        cid = credential.get("id") or credential.get("rawId")
        c = self.creds.get(cid)
        if c is None:
            raise PasskeyError("unknown_credential")
        try:
            v = verify_authentication_response(credential=credential, expected_challenge=challenge_for(digest),
                                               expected_rp_id=self.rp_id, expected_origin=origin or self.origins,
                                               credential_public_key=b64u_decode(c.public_key),
                                               credential_current_sign_count=c.sign_count)
        except Exception as e:
            raise PasskeyError(f"assertion_invalid: {e}")
        with self._lock:
            c.sign_count = v.new_sign_count
            self._save()
        return {"kind": "webauthn", "digest": digest, "credential_id": cid, "user": c.user,
                "user_verified": bool(v.user_verified), "sign_count": v.new_sign_count,
                "assertion": credential}


class PasskeyError(Exception):
    pass


class SoftAuthenticator:
    """A software WebAuthn authenticator (ES256, 'none' attestation, user present and verified)."""

    def __init__(self, rp_id: str = "localhost", origin: str = "http://localhost:8000", seed: bytes | None = None):
        self.rp_id = rp_id
        self.origin = origin
        if seed:
            n = int.from_bytes(hashlib.sha256(seed).digest(), "big") % (2 ** 255) + 1
            self.key = ec.derive_private_key(n, ec.SECP256R1())
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
        point = self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        self.cred_id = hashlib.sha256(point).digest()[:16]
        self.count = 0

    def _cose(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def _client_data(self, typ: str, challenge: bytes) -> bytes:
        return json.dumps({"type": typ, "challenge": b64u(challenge), "origin": self.origin,
                           "crossOrigin": False}, separators=(",", ":")).encode()

    def create(self, options: dict) -> dict:
        ch = b64u_decode(options["challenge"])
        cd = self._client_data("webauthn.create", ch)
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        att = b"\x00" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose()
        auth_data = rp_hash + bytes([0x45]) + struct.pack(">I", self.count) + att  # UP | UV | AT
        ao = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = b64u(self.cred_id)
        return {"id": cid, "rawId": cid, "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "attestationObject": b64u(ao)}, "clientExtensionResults": {}}

    def get(self, options: dict) -> dict:
        ch = b64u_decode(options["challenge"])
        cd = self._client_data("webauthn.get", ch)
        # The signature counter must grow across process restarts, or the relying party rejects the assertion.
        self.count = max(self.count + 1, int(time.time()))
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + struct.pack(">I", self.count)
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        cid = b64u(self.cred_id)
        return {"id": cid, "rawId": cid, "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "authenticatorData": b64u(auth_data), "signature": b64u(sig),
                             "userHandle": None}, "clientExtensionResults": {}}

