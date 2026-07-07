# Winfetti Backend — build spec (hand this to Claude Code)

Goal: make rewards real. Server-authoritative coins, verifiable ad/offer revenue in, gift cards out. FastAPI + Postgres, same stack as Cronys. Phase 1 is a weekend of work.

## Core principle

The phone is a view, the server is the truth. Every coin that exists was written by the server into an append-only ledger, and every credit path is either (a) verified by a third-party signature (ads, offers, IAP) or (b) client-claimed but hard-capped. Redemptions deduct atomically and sit in a review queue before any real dollar moves.

## Stack

- FastAPI + SQLAlchemy 2 + Alembic, Postgres 16, Redis (rate limits)
- Deploy: Fly.io or Railway, one region, docker-compose for local
- Auth: anonymous device auth returning a JWT (upgrade path: Sign in with Apple)
- Fulfillment: Tremendous API (sandbox first). They handle W-9/1099 collection for US recipients, which removes most of the tax headache
- Payments: RevenueCat webhooks (iOS IAP), Stripe webhooks (web)

## Schema (Postgres)

```sql
users        (id uuid pk, device_id text unique, email text, email_verified bool,
              created_at, status text default 'active', risk_score int default 0)

ledger       (id bigserial pk, user_id fk, amount int,            -- +credit / -debit
              balance_after int, kind text, ref text,
              idem_key text unique not null, created_at)
  -- kind: game_win | checkin | mission | ad_reward | offer | cashback
  --       | order_bonus | achievement | redemption_hold | redemption_refund | admin_adjust

redemptions  (id uuid pk, user_id fk, sku text, usd numeric, coins int,
              status text default 'pending',   -- pending|approved|sent|denied
              tremendous_order_id text, created_at, reviewed_at)

ad_receipts  (id bigserial, user_id fk, network text, tx_id text unique,
              payload jsonb, verified bool, created_at)

purchases    (id uuid pk, user_id fk, product_id text, store_tx_id text unique,
              status text, created_at)
```

Balance = maintained column on users updated inside the same transaction as the ledger insert, with `SELECT ... FOR UPDATE` on the user row. Idempotency key is unique-constrained: webhook retries and client retries can never double-credit. Write the test for this first.

## Credit paths

1. **Game wins (client-claimed, server-capped).** Client POSTs `{game, event, idem_key}`. Server owns the payout tables and multipliers (happy hour by server clock, gold from purchases table, boost from purchases). Hard caps: max single win per game (wheel 25,000 base), daily game-win cap per user (60,000), daily total credit cap (100,000). Anything over cap credits the cap and flags risk_score.
2. **Rewarded ads (verified).** AdMob Server-Side Verification: Google calls your endpoint with query params + ECDSA signature. Verify against Google's published verifier keys (https://www.gstatic.com/admob/reward/verifier-keys.json), check tx not seen, then credit. Pass user_id in SSV custom_data. No SSV callback, no coins, even if the client says the ad played.
3. **Offerwall (verified).** Tapjoy/AdGem/Fyber server-to-server postbacks with shared-secret hash. Same pattern: verify sig, unique tx_id, credit.
4. **IAP.** RevenueCat webhook grants `gold` entitlement and boost consumables; Stripe webhook for web. Never trust the client's "purchase succeeded".
5. **Cashback.** Affiliate network postbacks (Impact/Rakuten Advertising) credit pending; a scheduled job matures them after the return window.

## Redemption flow

```
POST /v1/redemptions {sku}
  -> gates: email_verified, account_age >= 7d, >= 10 verified ad_receipts,
            no other pending redemption, balance >= cost
  -> atomic: FOR UPDATE, ledger(-cost, redemption_hold), status=pending
Admin approves (first 2 per user manual, then auto under $10 if risk_score=0)
  -> POST Tremendous /api/v2/orders (email delivery, sandbox env first)
  -> status=sent; deny path refunds the hold
```

## API surface

```
POST /v1/auth/device            {device_id} -> {jwt}
GET  /v1/me                     balance, gold, boost_until, daily state
POST /v1/game/claim             {game, event, idem_key}
POST /v1/checkin                (server computes streak by its own clock)
POST /v1/redemptions            GET /v1/redemptions
GET  /v1/webhooks/admob-ssv     POST /v1/webhooks/{tapjoy,revenuecat,stripe,affiliate}
GET  /admin/redemptions?status=pending    POST /admin/redemptions/{id}/{approve,deny}
```

Rate limits (Redis): 30 writes/min/user, 5 auth/min/IP. Log every denial into a fraud_events table.

## Client migration (small)

In App.jsx, replace the storage autosave with an API sync layer: `award()` stays optimistic for feel, but posts the claim and reconciles from GET /v1/me. Keep a `DEMO_MODE` flag so the artifact keeps working offline. The AdModal already calls `window.crPlatform.showRewardedAd`; on real AdMob the SSV callback does the crediting, and the client just refreshes balance.

## Phases

- **P1 (weekend):** auth, ledger + idempotency tests, game/claim with caps, checkin, redemptions + admin queue, Tremendous sandbox. Rewards are real after this.
- **P2:** AdMob SSV, one offerwall postback, RevenueCat + Stripe webhooks.
- **P3:** email verification, affiliate postbacks + maturation job, fraud dashboard, Sentry.

## Repo layout for Claude Code

```
winfetti-api/
  CLAUDE.md            (paste the kickoff prompt below)
  app/{main,models,auth,ledger}.py  app/routes/  app/webhooks/  app/admin/
  tests/test_ledger.py  tests/test_redemptions.py
  alembic/  docker-compose.yml  .env.example
```

## Kickoff prompt for Claude Code

> Build Phase 1 of the spec in SPEC.md (this file). FastAPI + SQLAlchemy 2 + Postgres via docker-compose. Start with the ledger module and write pytest tests proving (1) duplicate idem_key never double-credits under concurrent requests and (2) redemption deduction is atomic under concurrent spends. Then auth, game/claim with the cap table, checkin with server-clock streaks, the redemption queue, and a minimal admin router. Use Tremendous's sandbox for fulfillment behind an interface so I can stub it in tests. Ship it runnable with `docker compose up` + `uvicorn`.

Sequencing note: domain terms like "fraud", "risk_score", and payout logic are legitimate here, but if a safety filter balks mid-session (you've hit this with Cronys vocabulary), splitting work into "ledger", "webhooks", "admin" sessions keeps context clean anyway.
