"""Run Visa's own TAP sample agent-registry and register Keystone's agent key in it.

Visa's code is fetched from github.com/visa/trusted-agent-protocol at setup time (its licence points to
the Visa Developer terms, so it is not copied into this repo). Once registered, start Keystone with
KEYSTONE_REGISTRY_URL=http://127.0.0.1:9002 and every merchant looks the agent's key up in Visa's registry.

    python scripts/visa_registry.py setup      clone + venv
    python scripts/visa_registry.py run        start the registry on :9002 (foreground)
    python scripts/visa_registry.py register   register the Keystone agent and its Ed25519 key
    python scripts/visa_registry.py deactivate deactivate the agent (the revocation demo)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "visa-tap"
REG = VENDOR / "agent-registry"
URL = "http://127.0.0.1:9002"
sys.path.insert(0, str(ROOT))


def venv_python() -> Path:
    return VENDOR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def setup() -> None:
    if not VENDOR.exists():
        subprocess.check_call(["git", "clone", "--depth", "1", "https://github.com/visa/trusted-agent-protocol.git",
                               str(VENDOR)])
    if not venv_python().exists():
        subprocess.check_call(["uv", "venv", "-q", "--python", "3.11", str(VENDOR / ".venv")])
    subprocess.check_call(["uv", "pip", "install", "-q", "--python", str(venv_python()), "fastapi==0.104.1",
                           "uvicorn==0.24.0", "python-dotenv", "sqlalchemy==2.0.23", "pydantic==2.5.0",
                           "email-validator"])
    print("ready:", REG)


def run() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "DATABASE_URL": f"sqlite:///{(VENDOR / 'keystone_registry.db').as_posix()}"}
    subprocess.call([str(venv_python()), "main.py"], cwd=str(REG), env=env)


def register() -> None:
    from keystone import tap
    key = tap.AgentKey.from_seed("keystone-agent-1", os.environ.get("KEYSTONE_AGENT_SEED", "keystone-demo").encode())
    r = httpx.post(f"{URL}/agents/register", json={
        "name": "Keystone buyer agent", "domain": os.environ.get("KEYSTONE_ORIGIN", "http://localhost:8000"),
        "description": "All-or-nothing basket coordinator", "is_active": "true",
        "keys": [{"key_id": key.key_id, "public_key": key.public_b64, "algorithm": "ed25519",
                  "description": "TAP signing key", "is_active": "true"}]})
    print(r.status_code, r.text[:300])
    print("GET /keys:", httpx.get(f"{URL}/keys/{key.key_id}").json())


def deactivate() -> None:
    agents = httpx.get(f"{URL}/agents").json()
    for a in agents:
        if a["name"] == "Keystone buyer agent":
            print(httpx.delete(f"{URL}/agents/{a['id']}").json())
            print("GET /keys after deactivation:", httpx.get(f"{URL}/keys/keystone-agent-1").status_code,
                  "(the sample still serves the key; Keystone's directory client also checks the agent)")


if __name__ == "__main__":
    {"setup": setup, "run": run, "register": register, "deactivate": deactivate}[sys.argv[1]]()
