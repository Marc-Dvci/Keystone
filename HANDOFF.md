# Handoff (20 Oct 2026)

State: 48 tests pass, 16 of 16 evidence cases pass, 4,800 simulated baskets with 0 errors. Live on the Airwallex sandbox: 20 of 20 probe steps, four full baskets (commit, abort with a rogue capture refused, kill and recovery, escalation explained by Gemini), see docs/LIVE_BASKETS.md. Credentials live only in the git-ignored `.env` (AWX_CLIENT_ID, AWX_API_KEY, AWS_PROFILE). Repo: github.com/Marc-Dvci/Keystone (MIT).

Live mode: `set -a; . ./.env; set +a; KEYSTONE_MODE=live KEYSTONE_DATA=runs/console_live python -m keystone.serve`. Holds go through Airwallex's Card Element in a headless browser (Native API needs PCI enablement); the sandbox acquirer rejects issued PANs, so the browser types the test card and the leg card carries the issuer side through the bridge (simulation by card_id; by card_number the sandbox answers EXPIRY_MISSING). SINGLE cards close after their first authorization; Keystone issues one card per attempt.

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

1. Advisor: Gemini 3.8 Flash on Vertex (`KEYSTONE_ADVISOR=gemini`, `GOOGLE_CLOUD_PROJECT`). Bedrock answers "Operation not allowed" on the AWS account tried.
2. Film and writeup are done outside the repo: `../film/remotion/out/keystone.mp4` (3:15, 2560x1440, captions in `../film/narration/narration.srt`, upload text in `../film/YOUTUBE.md`) and `../WRITEUP.md`. Remaining: upload the film and paste its link into the writeup and the README.

## Known limits

- The basket ledger lives in memory in the Keystone process. After a restart, a hold on an old basket's card is declined (card not in a basket), which fails closed.
- Live mode holds each payment intent with Airwallex's test card and mirrors the issuer side through the bridge: the sandbox acquirer rejects Airwallex-issued card numbers.
- Hardware scenario amounts stay under the 9,999.99 simulation cap per authorization.
