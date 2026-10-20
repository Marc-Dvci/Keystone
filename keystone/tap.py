"""Visa Trusted Agent Protocol: RFC 9421 HTTP message signatures with Ed25519.

The agent signs every request it sends a merchant. Phase requests (prepare, commit, abort) carry
tag="agent-payer-auth", one of the two tags Visa's spec says trigger trusted-agent checks. The body
is covered through a Content-Digest header (RFC 9530), so the basket ID, leg, phase and mandate
hash inside it are signed as well.

The verifier accepts the strict RFC 9421 serialisation and the variant produced by Visa's sample
agent (``; `` separators, ``keyId`` spelling), and looks keys up in a directory shaped like Visa's
sample agent-registry (``GET /keys/{key_id}``).
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

TAG_BROWSE = "agent-browser-auth"
TAG_PAY = "agent-payer-auth"
TAP_TAGS = {TAG_BROWSE, TAG_PAY}
LABEL = "sig2"
MAX_WINDOW_S = 480  # Web Bot Auth draft recommends short-lived signatures
CLOCK_SKEW_S = 60

COVERED_WITH_BODY = ("@method", "@authority", "@path", "content-type", "content-digest")
COVERED_NO_BODY = ("@method", "@authority", "@path")


class TapError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class AgentKey:
    key_id: str
    private: Ed25519PrivateKey

    @staticmethod
    def generate(key_id: str) -> "AgentKey":
        return AgentKey(key_id, Ed25519PrivateKey.generate())

    @staticmethod
    def from_seed(key_id: str, seed: bytes) -> "AgentKey":
        return AgentKey(key_id, Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed).digest()))

    @property
    def public_b64(self) -> str:
        raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode()


def content_digest(body: bytes) -> str:
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"


def _params(components: tuple[str, ...], created: int, expires: int, keyid: str, nonce: str, tag: str) -> str:
    comp = " ".join(f'"{c}"' for c in components)
    return f'({comp});created={created};expires={expires};keyid="{keyid}";alg="ed25519";nonce="{nonce}";tag="{tag}"'


def _component_value(name: str, method: str, authority: str, path: str, headers: dict[str, str]) -> str:
    if name == "@method":
        return method.upper()
    if name == "@authority":
        return authority.lower()
    if name == "@path":
        return path
    v = headers.get(name.lower())
    if v is None:
        raise TapError("missing_component", name)
    return v.strip()


def signature_base(components: tuple[str, ...], params: str, method: str, authority: str, path: str,
                   headers: dict[str, str]) -> str:
    lines = [f'"{c}": {_component_value(c, method, authority, path, headers)}' for c in components]
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines)


def sign_request(key: AgentKey, method: str, url: str, body: bytes | None, tag: str,
                 directory_url: str | None = None, ttl_s: int = 300, now: int | None = None,
                 nonce: str | None = None) -> dict[str, str]:
    """Return the headers to add to the request (Signature-Input, Signature, Content-Digest...)."""
    if tag not in TAP_TAGS:
        raise TapError("bad_tag", tag)
    u = httpx.URL(url)
    authority = u.netloc.decode()
    path = u.raw_path.decode().split("?")[0]
    created = int(now if now is not None else time.time())
    expires = created + ttl_s
    nonce = nonce or secrets.token_urlsafe(24)
    headers: dict[str, str] = {}
    if body is not None:
        headers["content-type"] = "application/json"
        headers["content-digest"] = content_digest(body)
        components = COVERED_WITH_BODY
    else:
        components = COVERED_NO_BODY
    params = _params(components, created, expires, key.key_id, nonce, tag)
    base = signature_base(components, params, method, authority, path, headers)
    sig = base64.b64encode(key.private.sign(base.encode())).decode()
    headers["signature-input"] = f"{LABEL}={params}"
    headers["signature"] = f"{LABEL}=:{sig}:"
    if directory_url:
        headers["signature-agent"] = f'"{directory_url}"'
    return headers


_SI_RE = re.compile(r'^\s*([A-Za-z0-9_-]+)=\(([^)]*)\)\s*(.*)$')


@dataclass
class ParsedInput:
    label: str
    components: tuple[str, ...]
    params_raw: str
    created: int
    expires: int
    keyid: str
    alg: str
    nonce: str
    tag: str


def parse_signature_input(value: str) -> ParsedInput:
    m = _SI_RE.match(value)
    if not m:
        raise TapError("bad_signature_input")
    label, comp_raw, rest = m.groups()
    components = tuple(c.strip('"') for c in comp_raw.split())
    attrs: dict[str, str] = {}
    for part in rest.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        attrs[k.strip().lower()] = v.strip().strip('"')
    try:
        created = int(attrs["created"])
        expires = int(attrs["expires"])
    except (KeyError, ValueError):
        raise TapError("missing_window")
    # Rebuild exactly the params string the signer serialised, as the signature base uses it verbatim.
    params_raw = value[len(label) + 1:].strip()
    return ParsedInput(label, components, params_raw, created, expires, attrs.get("keyid", ""),
                       attrs.get("alg", ""), attrs.get("nonce", ""), attrs.get("tag", ""))


class KeyDirectory(Protocol):
    async def get(self, key_id: str) -> dict | None: ...


class RegistryDirectory:
    """Client for a directory shaped like Visa's sample agent-registry: GET /keys/{key_id}."""

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None, ttl_s: int = 30):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=5)
        self.ttl_s = ttl_s
        self._cache: dict[str, tuple[float, dict | None]] = {}

    async def get(self, key_id: str) -> dict | None:
        hit = self._cache.get(key_id)
        if hit and time.time() - hit[0] < self.ttl_s:
            return hit[1]
        r = await self.client.get(f"{self.base_url}/keys/{key_id}")
        data = r.json() if r.status_code == 200 else None
        if data is not None and data.get("is_active", "true") != "true":
            data = None
        self._cache[key_id] = (time.time(), data)
        return data

    def forget(self, key_id: str) -> None:
        self._cache.pop(key_id, None)


class StaticDirectory:
    def __init__(self) -> None:
        self.keys: dict[str, dict] = {}

    def add(self, key: AgentKey, agent_name: str = "keystone-agent", active: bool = True) -> None:
        self.keys[key.key_id] = {"key_id": key.key_id, "public_key": key.public_b64, "algorithm": "ed25519",
                                 "is_active": "true" if active else "false", "agent_name": agent_name}

    async def get(self, key_id: str) -> dict | None:
        k = self.keys.get(key_id)
        if k is None or k.get("is_active") != "true":
            return None
        return k


class NonceStore(Protocol):
    def seen(self, nonce: str, expires: int) -> bool:
        """Record the nonce; return True if it was already recorded."""


class MemoryNonceStore:
    def __init__(self) -> None:
        self._n: dict[str, int] = {}

    def seen(self, nonce: str, expires: int) -> bool:
        now = int(time.time())
        for k in [k for k, e in self._n.items() if e < now - CLOCK_SKEW_S]:
            del self._n[k]
        if nonce in self._n:
            return True
        self._n[nonce] = expires
        return False


@dataclass
class Verified:
    key_id: str
    tag: str
    nonce: str
    created: int
    expires: int
    agent: dict = field(default_factory=dict)


async def verify_request(method: str, authority: str, path: str, headers: dict[str, str], body: bytes,
                         directory: KeyDirectory, nonces: NonceStore, require_tag: str | None = None,
                         now: int | None = None) -> Verified:
    h = {k.lower(): v for k, v in headers.items()}
    si, sg = h.get("signature-input"), h.get("signature")
    if not si or not sg:
        raise TapError("unsigned", "Signature-Input and Signature headers are required")
    p = parse_signature_input(si)
    if p.tag not in TAP_TAGS:
        raise TapError("bad_tag", p.tag or "missing")
    if require_tag and p.tag != require_tag:
        raise TapError("wrong_tag", f"expected {require_tag}, got {p.tag}")
    if p.alg and p.alg.lower() != "ed25519":
        raise TapError("bad_alg", p.alg)
    now = int(now if now is not None else time.time())
    if p.created > now + CLOCK_SKEW_S:
        raise TapError("created_in_future")
    if p.expires < now:
        raise TapError("expired")
    if p.expires - p.created > MAX_WINDOW_S:
        raise TapError("window_too_long")
    if not p.nonce:
        raise TapError("missing_nonce")
    # Visa's sample covers only @authority and @path; payer requests must also bind the method and body.
    needed = {"@authority", "@path"} | ({"@method"} if p.tag == TAG_PAY else set()) | (
        {"content-digest"} if body else set())
    if not needed.issubset(p.components):
        raise TapError("components_not_covered", ",".join(sorted(needed - set(p.components))))
    if body:
        if h.get("content-digest") != content_digest(body):
            raise TapError("digest_mismatch")
    key = await directory.get(p.keyid)
    if key is None:
        raise TapError("unknown_or_revoked_key", p.keyid)
    m = re.match(rf"^\s*{re.escape(p.label)}=:([^:]+):", sg)
    if not m:
        raise TapError("bad_signature_header")
    base = signature_base(p.components, p.params_raw, method, authority, path, h)
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(key["public_key"]))
        pub.verify(base64.b64decode(m.group(1)), base.encode())
    except (InvalidSignature, ValueError):
        raise TapError("bad_signature")
    # Nonce recorded only after the signature checks out, so forged requests cannot burn nonces.
    if nonces.seen(p.nonce, p.expires):
        raise TapError("replay", p.nonce)
    return Verified(p.keyid, p.tag, p.nonce, p.created, p.expires, key)
