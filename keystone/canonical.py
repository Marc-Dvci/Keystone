"""Canonical JSON (RFC 8785 subset: sorted keys, no whitespace, strings for decimals) and hashing."""
from __future__ import annotations

import base64
import hashlib
import json
from decimal import Decimal
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "to_json"):
        return o.to_json()
    raise TypeError(f"not serialisable: {type(o).__name__}")


def canonical(obj: Any) -> bytes:
    """Floats are refused: every amount travels as a decimal string."""
    _reject_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default).encode()


def _reject_floats(obj: Any) -> None:
    if isinstance(obj, float):
        raise TypeError("floats are not allowed in signed payloads; use decimal strings")
    if isinstance(obj, dict):
        for v in obj.values():
            _reject_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _reject_floats(v)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(obj: Any) -> str:
    return "sha256:" + sha256_hex(canonical(obj))


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
