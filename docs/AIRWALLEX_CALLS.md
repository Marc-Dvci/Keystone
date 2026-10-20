# Airwallex calls

Every call Keystone makes, in `keystone/airwallex/client.py`. Base URL `https://api.sandbox.airwallex.com/api/v1`; amounts in major units; one `request_id` per operation, reused only for a retry of that operation.

| Step | Call | Fields Keystone sends | What Keystone relies on |
|---|---|---|---|
| Auth | `POST /authentication/login` | `x-client-id`, `x-api-key` headers | 30-minute bearer token, refreshed a minute early |
| Merchant: hold | `POST /pa/payment_intents/create` | `amount`, `currency`, `merchant_order_id`, `request_id`, `metadata` (basket, leg, mandate digest, TAP key id) | `REQUIRES_PAYMENT_METHOD` |
| | `POST /pa/payment_intents/{id}/confirm` | `payment_method.card`, `payment_method_options.card.auto_capture: false`, `authorization_type: pre_auth` for lodging | `REQUIRES_CAPTURE`; pre_auth only with auto_capture false |
| Merchant: commit | `POST /pa/payment_intents/{id}/capture` | `request_id`, `amount` | one capture per intent, never above the hold; re-read with `GET /pa/payment_intents/{id}` |
| Merchant: abort | `POST /pa/payment_intents/{id}/cancel` | `request_id`, `cancellation_reason` | `CANCELLED`; capture afterwards is refused |
| Baseline only | `POST /pa/refunds/create` | `payment_intent_id`, `amount`, `request_id` | the sequential agent's unwind |
| Leg cards | `POST /issuing/cardholders/create` | INDIVIDUAL, synthetic details, `express_consent_obtained: "yes"` | `READY` in sandbox |
| | `POST /issuing/cards/create` | `form_factor: VIRTUAL`, `authorization_controls`: `allowed_transaction_count: SINGLE`, `transaction_limits` (PER_TRANSACTION and ALL_TIME, leg currency), `allowed_currencies`, `allowed_merchant_categories`, `active_from`, `active_to` | `ACTIVE`; declines with `LIMIT_EXCEEDED`, `CURRENCY_NOT_ALLOWED`, `MERCHANT_CATEGORY_NOT_ALLOWED`, `OUT_OF_ALLOWED_TIME_RANGE` |
| | `GET /issuing/cards/{id}/details` | | PAN, expiry, CVV for the per-leg credential |
| | `POST /issuing/cards/{id}/update` | `card_status: CLOSED` | every card closed after commit or abort |
| Issuer side of each hold | `POST /simulation/issuing/create` | `card_number`, `transaction_amount`, `transaction_currency`, `merchant_category_code`, `merchant_info` | lifecycle id for later capture or reversal |
| | `POST /simulation/issuing/card_transaction_lifecycles/{id}/capture` and `/reverse` | `transaction_amount` | clearing on commit, reversal on abort |
| | `POST /simulation/issuing/refund` | lifecycle id, amount, currency | baseline only |
| | `GET /issuing/transactions?card_id=` | | the card ledger in the console |
| FX lock | `POST /fx/quotes/create` | `sell_currency` (home), `buy_currency` (leg), `buy_amount`, `validity` from MIN_15 to HR_24, chosen to cover the commit window | `quote_id`, `client_rate`, `valid_to_at` |
| FX at commit | `POST /fx/conversions/create` | `quote_id`, currencies, dealt amount, `request_id` | settles at the quote's `client_rate` |
| | `GET /fx/rates/current`, `GET /balances/current` | | baseline refunds, console |
| Remote authorization | Airwallex calls Keystone's `/issuer/remote-auth` | v2 body; `x-nonce`, base64 HMAC-SHA256 `x-signature` | answer within 2.5 s, echo `card_transaction_event_id` |

Behaviour the live probe checks on the sandbox (`scripts/live_probe.py` writes `docs/LIVE_SANDBOX.md`): hold, pre_auth hold, capture, second capture refused, void, capture after void refused, capture above the hold refused, partial capture, HR_24 quote and conversion at its rate, card controls declines, a SINGLE card after a reversal, whether an issued card can pay an Airwallex payment intent, and a closed card.
