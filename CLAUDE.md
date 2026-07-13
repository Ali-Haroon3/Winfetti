# Winfetti API

Server-authoritative rewards backend. Full product spec in `SPEC.md` — read it
before changing anything about coins, caps, or redemptions.

## Core principle

The phone is a view, the server is the truth. Every coin is a row in the
append-only `ledger` table, written under `SELECT ... FOR UPDATE` on the user
row with a unique `idem_key`. `users.balance` is a maintained column updated in
the same transaction. Never write a balance without going through
`app/ledger.py`.

## Layout

- `app/ledger.py` — the only way coins move; idempotency + row locking
- `app/services.py` — game claims (caps/multipliers), check-in streaks, redemption lifecycle
- `app/payouts.py` — server-owned payout tables, check-in rewards, redemption catalog
- `app/routes/`, `app/admin/` — HTTP layer; routes commit, services lock.
  One deliberate exception: `approve_redemption` commits mid-service so the
  `approved` state is durable (and its row lock released) before the
  external Tremendous call — don't "fix" that.
- `app/webhooks/` — verified credit paths: AdMob SSV (ECDSA), Tapjoy
  (shared-secret hash), RevenueCat/Stripe (purchase entitlements). Each
  endpoint 503s until its secret is configured.
- `app/jobs.py`, `app/scheduler.py` — maintenance jobs (cashback maturation,
  stuck-redemption retry, token purge) run in-process from the lifespan and
  must stay safe under concurrent runs (SKIP LOCKED + idempotency). Also
  runnable via `python -m app.jobs ...` and `POST /admin/jobs/...`.
- `app/fulfillment.py` — Tremendous behind a Protocol; stub used when no API key
- `app/emailer.py` — verification email seam; SMTP when configured, else logs
- `app/clock.py` — the server clock seam; never call `datetime.now()` elsewhere

## Commands

```bash
# run everything
docker compose up

# local dev
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload

# tests (need a local Postgres; see tests/conftest.py for the URL)
pytest
```

## Conventions

- Tests hit a real Postgres — the concurrency tests in `tests/test_ledger.py`
  are the contract; keep them passing before anything else.
- New credit paths: verify a third-party signature or hard-cap the amount.
  There is no third option.
- All time-based rules read `app/clock.py` (never client timestamps).
- Schema changes go through Alembic (`alembic revision --autogenerate`);
  CI fails if models and migrations diverge (`scripts/check_migrations.py`).
