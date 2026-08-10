# Winfetti API

Server-authoritative rewards backend for Winfetti: coins live in an
append-only Postgres ledger, credits are either third-party-verified or
hard-capped, and gift-card redemptions sit in a review queue before any real
dollar moves (fulfilled via [Tremendous](https://www.tremendous.com/), sandbox
by default). See `SPEC.md` for the full design.

## Quick start

```bash
docker compose up          # Postgres 16 + Redis + API on :8000
```

The API container runs `alembic upgrade head` on boot. Without a
`TREMENDOUS_API_KEY` the app uses an in-memory fulfillment stub, so nothing
real can be paid out from a dev environment.

Local (no Docker):

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env       # point DATABASE_URL/REDIS_URL at your services
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

## Tests

The test suite runs against a real Postgres (the concurrency tests exercise
`SELECT ... FOR UPDATE` semantics SQLite can't fake):

```bash
createdb winfetti_test     # postgresql://winfetti:winfetti@localhost:5432/winfetti_test
.venv/bin/pytest
```

`tests/test_ledger.py` proves the two core invariants under concurrent
requests: a duplicate `idem_key` never double-credits, and debits never drive
a balance negative. `tests/test_redemptions.py` proves the redemption hold is
atomic and the review queue (approve → Tremendous order → sent, deny →
refund) behaves.

## API

```
POST /v1/auth/device            {device_id} -> {jwt}        (5/min/IP)
GET  /v1/me                     balance, gold, boost_until, daily state
GET  /v1/me/ledger              coin history (opaque ?cursor=, ?kind= filter)
GET  /v1/me/cashback            cashback credits + pending total, matures_at
POST /v1/game/claim             {game, event, idem_key}     (writes: 30/min/user)
POST /v1/checkin                server-clock streaks
POST /v1/me/email               {email} -> verification email (token, 24h TTL)
POST /v1/me/email/verify        {token} -> email_verified
POST /v1/redemptions            {sku}   GET /v1/redemptions
GET  /v1/redemptions/catalog
GET  /v1/webhooks/admob-ssv     AdMob SSV (ECDSA-verified rewarded ads)
GET  /v1/webhooks/tapjoy        offerwall postback (shared-secret hash)
POST /v1/webhooks/revenuecat    iOS IAP -> gold/boost entitlements
POST /v1/webhooks/stripe        web purchases (Stripe-Signature verified)
GET  /v1/webhooks/affiliate     cashback postbacks (HMAC; pending -> matured)
GET  /admin/redemptions?status=pending          (X-Admin-Key header)
POST /admin/redemptions/{id}/approve  /deny
GET  /admin/fraud/events        GET /admin/fraud/summary
GET  /admin/users/{id}          POST /admin/users/{id}/status {active|banned}
GET  /admin/users/{id}/ledger   full audit view (idem keys included)
POST /admin/users/{id}/adjust   {amount, reason, idem_key}  manual credit path
GET  /admin/stats/economy       coin liability, redemption dollars, daily flow
POST /admin/jobs/mature-cashback
POST /admin/jobs/audit-ledger   prove users.balance against the ledger
```

Redemption gates: verified email, account ≥ 7 days old, ≥ 10 verified ad
receipts, no other pending redemption, a 60s cooldown since the user's last
redemption (POST carries no idem_key, so this is what stops a network retry
from shipping twice), and sufficient balance. The first two redemptions per
user are approved manually; after that, orders under $10 from zero-risk users
auto-approve. A fulfillment failure leaves the redemption in `approved` —
list those with `?status=approved` and re-approve to retry (Tremendous
dedupes on `external_id`, so a retry can never pay twice; deny is only valid
from `pending`, because an `approved` row may already have an order in
flight). Rate-limit, gate, and auth denials are all recorded in
`fraud_events`.

Deployment note: `TRUST_PROXY_HEADERS` defaults to true, which assumes
exactly one edge proxy (Fly/Railway) appending the client IP to
`X-Forwarded-For`. If you expose the app directly, set it to false or the
per-IP auth rate limit becomes spoofable. Builds install from
`requirements.lock` (exact pins); `requirements.txt` holds the human-edited
floors — refresh the lock after changing it.

Webhook credit paths (Phase 2) are live but each returns 503 until its
secret is configured (`TAPJOY_SECRET`, `REVENUECAT_WEBHOOK_AUTH`,
`STRIPE_WEBHOOK_SECRET`); AdMob SSV verifies ECDSA signatures against
Google's published verifier keys and needs no secret. Ad rewards pay the
server-owned `AD_REWARD_COINS` (the callback's reward params are logged,
never trusted); offerwall amounts are clamped to
`MAX_OFFER_COINS_PER_POSTBACK`. Pass the user's uuid in AdMob's
`custom_data`, Tapjoy's `snuid`, RevenueCat's `app_user_id`, and Stripe
checkout `metadata.user_id`.

Cashback (Phase 3): affiliate postbacks (`AFFILIATE_SECRET`-signed) create
*pending* credits worth `commission * CASHBACK_SHARE`; coins reach the
ledger only when the maturation job runs after the
`CASHBACK_MATURATION_DAYS` return window — schedule
`python -m app.jobs mature-cashback` from cron (concurrent runs are safe:
SKIP LOCKED + ledger idempotency). Reversals cancel pending credits;
reversals arriving after maturity are flagged in `fraud_events`, not clawed
back. Email verification tokens are stored hashed with a 24h TTL and are
single-use; a verified email is exclusive to one account. Verification
emails go through the `EmailSender` seam in `app/emailer.py` — wire a real
provider there (the default just logs). Set `SENTRY_DSN` to enable Sentry.
The fraud dashboard is JSON-only for now: recent events, counts by
kind/IP, top risk-scored users, per-user drilldown, ban/unban.

Manual balance corrections go through `POST /admin/users/{id}/adjust` — the
same ledger path as everything else (row lock + idem_key), capped at
`ADMIN_ADJUST_MAX_COINS` per call either direction, with every real move
logged to `fraud_events` as the audit trail. The staff-written reason is
admin-only: the user-facing history masks `ref` for every kind that isn't
the user's own data (game outcome, streak, redemption id), so support notes
and network tx ids never reach the client. `python -m app.jobs
audit-ledger` (or `POST /admin/jobs/audit-ledger`) proves
`users.balance == SUM(ledger.amount)` and the full `balance_after` chain
(every entry, not just the newest — a corrupted row buried by later writes
still surfaces); candidates from the lock-free scan are re-checked under
the user row lock, confirmed mismatches land in `fraud_events`, and the
CLI exits non-zero so a cron alert fires.
