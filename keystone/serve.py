"""Start Keystone: the merchants' process and the Keystone process, and restart Keystone if it is
killed (the crash demo exits with code 75; recovery then runs from the durable log).

    python -m keystone.serve            replay mode, no credentials needed
    KEYSTONE_MODE=live python -m keystone.serve   Airwallex sandbox (AWX_CLIENT_ID, AWX_API_KEY)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

CRASH_EXIT = 75


def main() -> None:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    world = subprocess.Popen([sys.executable, "-m", "keystone.world_server"], env=env)
    print("Keystone console: http://localhost:8000", flush=True)
    restarts = 0
    try:
        while True:
            app = subprocess.Popen([sys.executable, "-m", "keystone.app"], env=env)
            code = app.wait()
            if code == CRASH_EXIT:
                restarts += 1
                print(f"coordinator exited mid-basket (restart {restarts}); restarting, recovery reads the log",
                      flush=True)
                time.sleep(1.5)
                continue
            break
    except KeyboardInterrupt:
        pass
    finally:
        for p in (world,):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM) if os.name != "nt" else p.terminate()


if __name__ == "__main__":
    main()
