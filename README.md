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

CI (`.github/workflows/ci.yml`) runs the same suite against Postgres 16 +
Redis 7 on every push, after proving the alembic chain applies cleanly and
still matches `app/models.py` (`scripts/check_migrations.py`).

## Background jobs

The API schedules its own maintenance from the process lifespan
(`app/scheduler.py`), so `docker compose up` — or one deployed machine — is
fully self-operating with no external cron:

- **mature-cashback** (hourly): credits pending cashback whose return window
  has passed.
- **retry-approved** (every 5 min): re-drives fulfillment for redemptions
  stranded in `approved` by a fulfillment failure or crash. Tremendous
  dedupes on `external_id`, so a retry can never pay twice; rows younger
  than `REDEMPTION_RETRY_STUCK_AFTER_SECONDS` (default 15 min) are left
  alone so an in-flight order is never double-called.
- **purge-email-tokens** (daily): drops verification tokens that expired
  more than `EMAIL_TOKEN_PURGE_AFTER_DAYS` ago.

Every job is safe under concurrent runs (SKIP LOCKED row claims + ledger
idempotency + fulfillment dedupe), so running several replicas is fine. Set
`SCHEDULER_ENABLED=false` (or an individual interval to 0) to turn it off —
e.g. when a separate worker runs the same jobs via cron:
`python -m app.jobs {mature-cashback|retry-approved|purge-email-tokens}`.
Each job is also triggerable via `POST /admin/jobs/...` for ops.

## Frontends

Two no-build pages ship inside the image and are served by the API itself
(same origin, no CORS, no node toolchain):

- **`/` — the player app.** Anonymous device auth, the prize wheel (built
  from `GET /v1/config`, so segment amounts always match the server's
  payout tables), daily check-in, cap meters, email verification, and the
  redemption catalog with live status. Denial codes surface as plain
  sentences.
- **`/console` — the ops console.** Unlocked by the `ADMIN_API_KEY`
  (kept in sessionStorage): redemption queue with approve/deny, the fraud
  dashboard, user drilldown with ban/unban, and manual job triggers.

Design tokens live in `web/static/theme.css` (dark ledger direction, one
gold accent, mono numbers, self-hosted fonts). Both pages are static files
in `web/`; there is nothing to compile.

## API

```
GET  /                          player web app
GET  /console                   ops console (unlocked by ADMIN_API_KEY)
POST /v1/auth/device            {device_id} -> {jwt}        (5/min/IP)
GET  /v1/me                     balance, gold, boost_until, daily state
GET  /v1/config                 public payout constants (wheel, check-in)
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
POST /admin/jobs/{mature-cashback,retry-approved,purge-email-tokens}
```

Redemption gates: verified email, account ≥ 7 days old, ≥ 10 verified ad
receipts, no other pending redemption, a 60s cooldown since the user's last
redemption (POST carries no idem_key, so this is what stops a network retry
from shipping twice), and sufficient balance. The first two redemptions per
user are approved manually; after that, orders under $10 from zero-risk users
auto-approve. A fulfillment failure leaves the redemption in `approved`; the
scheduled retry-approved job re-drives those automatically (Tremendous
dedupes on `external_id`, so a retry can never pay twice; deny is only valid
from `pending`, because an `approved` row may already have an order in
flight — they also remain listable with `?status=approved` and manually
re-approvable). Rate-limit, gate, and auth denials are all recorded in
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
`CASHBACK_MATURATION_DAYS` return window (the in-process scheduler runs it
hourly — see Background jobs). Reversals cancel pending credits; reversals
arriving after maturity are flagged in `fraud_events`, not clawed back.
Email verification tokens are stored hashed with a 24h TTL and are
single-use; a verified email is exclusive to one account. Verification
emails send over SMTP once `SMTP_HOST` + `EMAIL_FROM` are set (any
SES/Postmark/Mailgun SMTP endpoint works; `EMAIL_VERIFY_LINK_TEMPLATE`
optionally embeds a deep link) — without them the dev sender just logs, and
no one can pass the email gate in production. Set `SENTRY_DSN` to enable
Sentry. The fraud dashboard is JSON-only for now: recent events, counts by
kind/IP, top risk-scored users, per-user drilldown, ban/unban.
