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
POST /v1/game/claim             {game, event, idem_key}     (writes: 30/min/user)
POST /v1/checkin                server-clock streaks
POST /v1/redemptions            {sku}   GET /v1/redemptions
GET  /v1/redemptions/catalog
GET  /admin/redemptions?status=pending          (X-Admin-Key header)
POST /admin/redemptions/{id}/approve  /deny
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

Phase 2 (not yet built): AdMob SSV, offerwall postbacks, RevenueCat/Stripe
webhooks — the `ad_receipts` and `purchases` tables they write to already
exist.
