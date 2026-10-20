# Visa in Keystone

## Trusted Agent Protocol

`keystone/tap.py`. Every request the agent sends a merchant carries an RFC 9421 HTTP message signature made with an Ed25519 key.

- Browsing a catalog: `tag="agent-browser-auth"`, covering `@method`, `@authority`, `@path`.
- Prepare, commit, abort, refund: `tag="agent-payer-auth"`, also covering `content-type` and an RFC 9530 `content-digest` of the body. The basket, leg, offer, amount, mandate digest and phase are inside the body, so they are signed; a merchant also refuses a body whose `phase` differs from the route.
- Parameters: `created`, `expires` (window at most 8 minutes), `keyid`, `alg="ed25519"`, `nonce`, `tag`. A `Signature-Agent` header names the key directory.
- The merchant verifier rejects a missing or unknown tag, a browse tag on a payer route, an expired or future signature, a window that is too long, an uncovered component, a body that does not match its digest, an unknown or revoked key, and a replayed nonce. Nonces are recorded only after the signature verifies.
- The parser also accepts the serialisation of Visa's sample agent (`; ` separators, `keyId`), tested in `tests/test_tap.py`.

Key directory: Keystone's own endpoint answers `GET /registry/keys/{id}` in the shape of Visa's sample agent-registry, and `scripts/visa_registry.py` runs Visa's registry itself. Visa's sample keeps serving `GET /keys/{id}` after the agent is deactivated, so Keystone's directory client checks the agent's status as well and fails closed (`tests/test_registry.py`).

## Visa Intelligent Commerce

`keystone/vic.py`, a stand-in with the field names of the CyberSource Intelligent Commerce guide, because VIC access is pilot-gated.

- `POST /acp/v1/instructions`: one instruction for the basket, one entry in `mandates[]` per leg (`mandateId`, `description`, `declineThreshold`, `effectiveUntilTime`, `preferredMerchantName`, `quantity`), the passkey result in `assuranceData[].authenticatedIdentities` with provider `VISA_PAYMENT_PASSKEY`. The guide places the assertion next to the mandates without a field that binds the two. Keystone binds them: the WebAuthn challenge is the digest of the canonical basket, carried in a `keystone` object, and the service refuses mandates that differ from that basket.
- `POST /acp/v1/instructions/{id}/credentials`: releases the leg's Airwallex card only for that leg, in its currency, at or under its decline threshold, before its expiry.
- `PUT /acp/v1/instructions/{id}`: an amendment after an escalation, approved by a passkey assertion over the amendment's own digest. It raises one leg's threshold and nothing else.
- `POST /acp/v1/instructions/{id}/confirmations`: each leg's outcome (`COMPLETED` or `CANCELLED`), answered with a signed payload.

## Visa Core Rules

`keystone/rules.py`, from the public edition of 18 April 2026.

| Rule | Check |
|---|---|
| Table 5-12, authorization validity | card-absent customer-initiated 10 days, 30 with the extended indicator or for lodging, cruise and vehicle rental, merchant-initiated 5. A merchant refuses a hold whose commit deadline is beyond that window; a capture past it fails the check |
| Table 5-14, reversal within 24 hours | each void receipt records the time from the abort request to the reversal |
| 1.7.7.2, no deposit after a full reversal | a merchant refuses a commit on a voided leg; Airwallex refuses the capture (evidence case "Merchant captures after it was told to abort") |
| 5.7.3.1, no split transactions | one authorization per leg per merchant; the issuer ledger declines a second hold on a leg |
