# Baskets run on the Airwallex sandbox

20 October 2026, `KEYSTONE_MODE=live python -m keystone.serve`, Lisbon scenario, against https://api.sandbox.airwallex.com. Every payment intent and card below exists in the sandbox account.

How a hold is placed in live mode: the merchant creates the payment intent on its server, and the agent's headless browser opens the merchant's checkout page, TAP-signed, and types the card into Airwallex's own Card Element with `autoCapture: false` (`pre_auth` for the hotel). Server-to-server card entry needs PCI-DSS enablement, which this account does not have. Airwallex's sandbox acquirer rejects an Airwallex-issued card number ("invalid pan"), so the browser types Airwallex's test card while each leg's own Airwallex card carries the issuer side of the same authorization through Keystone's network bridge (issuing simulation by `card_id`, decided by the basket ledger).

## 1. Every leg held, then committed

Basket `bkt_a575b75d78bb`, 75 s end to end (four browser checkouts), $8,627.85 charged against a $9,400 cap, at the quote-locked rates.

| Leg | Payment intent | Status | Captured | Leg card | Card ledger |
|---|---|---|---|---|---|
| dinner | int_sgpvm8gfjhne2x53xie | SUCCEEDED | EUR 510 | •1287 CLOSED | CLEARING 510 |
| hotel | int_sgpvm8gfjhne2xbbs5u | SUCCEEDED | EUR 1,890 | •2003 CLOSED | CLEARING 1,890 |
| flights | int_sgpvm8gfjhne2xg83ee | SUCCEEDED | USD 3,540 | •3688 CLOSED | CLEARING 3,540 |
| passes | int_sgpvm8gfjhne2xm0u2h | SUCCEEDED | EUR 2,070 | •6708 CLOSED | CLEARING 2,070 |

## 2. The summit sells out after three legs are held; the hotel then tries to capture

Basket `bkt_b88f2b5a29d4`: aborted, $0.00 charged, three voids in 5.3 s.

| Leg | Payment intent | Status | Leg card | Card ledger |
|---|---|---|---|---|
| dinner | int_sgpvf2z9fhne30ipp0e | CANCELLED | •1047 CLOSED | AUTHORIZATION REVERSED |
| hotel | int_sgpvm8gfjhne30qudg1 | CANCELLED | •9651 CLOSED | AUTHORIZATION REVERSED |
| flights | int_sgpvf2z9fhne30x0oec | CANCELLED | •7614 CLOSED | AUTHORIZATION REVERSED |
| passes | none (sold out at prepare) | | •0259 CLOSED | none |

The hotel's capture on its voided intent, sent by the merchant after the abort, was refused by Airwallex: `invalid_status_for_operation`, "The PaymentIntent status CANCELLED is invalid for operation capture."

## 3. The coordinator process is killed in the middle of the holds

Basket `bkt_b01da7b8ae35`. The process exited at 15:04:37.751 while preparing the flights; dinner and hotel were held. The supervisor restarted it; at 15:04:49.243 recovery found the basket with no commit decision and aborted it.

| Leg | Payment intent | Status after recovery | Leg card |
|---|---|---|---|
| dinner | int_sgpvm8gfjhne33ysb5l | CANCELLED (presumed_abort) | CLOSED, AUTHORIZATION REVERSED |
| hotel | int_sgpvf2z9fhne3451ctp | CANCELLED (presumed_abort) | CLOSED, AUTHORIZATION REVERSED |
| flights | none: the prepare was logged but never sent; the merchant now refuses it | | CLOSED |
| passes | none | | CLOSED |

The probe of each Airwallex behaviour these runs rely on is in [LIVE_SANDBOX.md](LIVE_SANDBOX.md).
