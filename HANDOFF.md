# Handoff (20 Oct 2026)

State: built and tested in replay mode. 46 tests pass, 16 of 16 evidence cases pass, 4,800 simulated baskets with 0 errors. Not yet run against the Airwallex sandbox (no keys yet). Local git only, not pushed.

## Run

```
.venv/Scripts/python -m keystone.serve          # console at http://localhost:8000
.venv/Scripts/python -m pytest
.venv/Scripts/python -m keystone.evidence       # rewrites docs/EVIDENCE.md
.venv/Scripts/python -m keystone.sim --n 400 --sweep --out runs/sim ; python scripts/figures.py
python scripts/drive_console.py --out docs/shots   # headless Chromium, virtual passkey; needs serve running
```

`serve` starts two processes: `keystone.world_server` (merchants on 8101-8116, Airwallex simulator on 8090) and `keystone.app` (console, coordinator, ledger on 8000). The crash event makes `keystone.app` exit with code 75; `serve` restarts it and recovery runs from `runs/console/journal.sqlite`.

## Open items

1. Airwallex sandbox keys, then `scripts/live_probe.py`, then a live basket (`KEYSTONE_MODE=live`). Things only the sandbox can answer: whether a SINGLE card accepts a second authorization after a reversal (Keystone already issues one card per attempt), whether an issued card can pay an Airwallex payment intent (the network bridge covers the case where it cannot), the exact simulation capture/reverse path (the client tries the lifecycle path, then the transaction path), and whether `pre_auth` holds behave in sandbox as documented.
2. Anthropic key for the advisor (`KEYSTONE_ADVISOR=1`); without it the rule-written explanation is shown.
3. Film, writeup, gallery; repo publishing and licence.

## Known limits

- The basket ledger lives in memory in the Keystone process. After a restart, a hold on an old basket's card is declined (card not in a basket), which fails closed.
- Live mode holds each payment intent with Airwallex's test card and mirrors the issuer side through the bridge, unless the closed-loop probe shows issued cards can pay intents.
- Hardware scenario amounts stay under the 9,999.99 simulation cap per authorization.
