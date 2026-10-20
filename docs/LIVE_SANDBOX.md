# Airwallex sandbox probe

Run 2026-10-20 15:06 UTC against https://api.sandbox.airwallex.com, 46 API calls, 20 of 20 steps as expected.

| Step | Expected | Observed | |
|---|---|---|---|
| login | a bearer token | bearer token received (not recorded) | ok |
| balances | a balance per currency | {'AED': Decimal('0'), 'ARS': Decimal('0'), 'AUD': Decimal('0'), 'BDT': Decimal('0'), 'BHD': Decimal('0'), 'BOB': Decimal('0'), 'BRL': Decimal('0'), 'CAD': Decimal('0'), 'CHF': Decimal('0'), 'CLP': Decimal('0'), 'CNY': Decimal('10000000'), 'COP': Decimal('0'), 'CZK': Decimal('0'), 'DKK': Decimal('0'), 'EGP': Decimal('0'), 'EUR': Decimal('10001790'), 'GBP': Decimal('10000000'), 'HKD': Decimal('0'),  | ok |
| hold (Card Element, autoCapture false) | REQUIRES_CAPTURE | int_sgpvm8gfjhne35503fe REQUIRES_CAPTURE | ok |
| hold with pre_auth (EUR) | REQUIRES_CAPTURE | int_sgpvm8gfjhne35a2kvt REQUIRES_CAPTURE | ok |
| capture the hold | SUCCEEDED | SUCCEEDED captured 123.45 | ok |
| second capture on the same intent | refused | refused invalid_status_for_operation | ok |
| void (cancel) a held intent | CANCELLED | CANCELLED | ok |
| capture after void (Visa 1.7.7.2) | refused | refused invalid_status_for_operation: The PaymentIntent status CANCELLED is invalid for operation capture. | ok |
| capture above the authorized amount | refused | refused amount_above_limit | ok |
| partial capture | SUCCEEDED, captured 55 | SUCCEEDED 55 | ok |
| FX quote USD->EUR, HR_24 | quote_id and valid_to ~24 h out | 9519fb76-8571-3f03-b1f0-5f6351a80f16 client_rate 1.138771 sell 2152.28 valid_to 2026-10-21 15:05:56+00:00 | ok |
| convert at the locked quote | same client_rate | 86efead9-230d-4aa0-85c9-029b6c9aec15 SETTLED rate 1.138771 | ok |
| cardholder | READY | ec9d2325-a4a5-4413-a143-e6d16d4ec73a | ok |
| leg card: SINGLE, EUR, MCC 7011, limit 2000, 2 h window | ACTIVE | c99a7839-0853-417c-be7a-e5004f4f9771 ACTIVE •2809 | ok |
| authorization inside the controls | approved | 01a159d9-11de-7000-a385-50810b4b5d43 PENDING None | ok |
| reverse it | reversal | REVERSAL_AUTH APPROVED | ok |
| controls decline: wrong MCC, over limit | MERCHANT_CATEGORY_NOT_ALLOWED, LIMIT_EXCEEDED | wrong MCC -> MERCHANT_CATEGORY_NOT_ALLOWED; over limit -> LIMIT_EXCEEDED | ok |
| SINGLE card after a reversal | recorded (decides whether Keystone needs a card per attempt) | second authorization on a SINGLE card after a reversal: FAILED CARD_CLOSED | ok |
| closed loop: issued card pays a payment intent | recorded | not accepted (REQUIRES_PAYMENT_METHOD: {"code":"validation_error","message":"Invalid input. Please verify your payment details an); Keystone's network bridge carries the issuer side instead | ok |
| close the card, then try it | CARD_CLOSED | after close: CARD_CLOSED | ok |
