---
name: verify
description: Build/launch/drive recipe for verifying Winfetti API changes end-to-end against a real Postgres.
---

# Verifying Winfetti API changes

## Environment setup (once per container)

```bash
pg_ctlcluster 16 main start   # Ubuntu's bundled Postgres 16; no Docker needed
sudo -u postgres psql -c "CREATE ROLE winfetti LOGIN PASSWORD 'winfetti' CREATEDB;"
sudo -u postgres createdb -O winfetti winfetti
sudo -u postgres createdb -O winfetti winfetti_test
redis-server --port 6379 --save '' --daemonize yes
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
DATABASE_URL="postgresql+psycopg://winfetti:winfetti@localhost:5432/winfetti" .venv/bin/alembic upgrade head
```

## Launch

```bash
DATABASE_URL="postgresql+psycopg://winfetti:winfetti@localhost:5432/winfetti" \
REDIS_URL="redis://localhost:6379/0" \
JWT_SECRET="local-verify-secret-0123456789abcdef01234567" \
ADMIN_API_KEY="admin-key-local" \
.venv/bin/uvicorn app.main:app --port 8000
```

No `TREMENDOUS_API_KEY` → in-memory stub fulfillment (order ids look like
`stub-<uuid>`), so approvals are safe to drive.

## Flows worth driving

```bash
curl -s -X POST :8000/v1/auth/device -d '{"device_id":"verify-device-alpha-001"}'  # -> jwt
# claim + replay same idem_key (second must be awarded:0, replay:true)
curl -s -X POST :8000/v1/game/claim -H "Authorization: Bearer $T" \
  -d '{"game":"wheel","event":"seg_500","idem_key":"k-...unique..."}'
curl -s -X POST :8000/v1/checkin -H "Authorization: Bearer $T"   # twice: 2nd already_checked_in
```

Redemption needs a seeded-eligible user (email_verified, 30d old, 10 verified
ad receipts, balance via a ledger row — keep users.balance == sum(ledger)):

```sql
UPDATE users SET email='v@example.com', email_verified=true, created_at=now()-interval '30 days' WHERE id='<uid>';
INSERT INTO ad_receipts (user_id,network,tx_id,verified) SELECT '<uid>','admob','tx-'||g,true FROM generate_series(1,10) g;
UPDATE users SET balance=balance+100000 WHERE id='<uid>';
INSERT INTO ledger (user_id,amount,balance_after,kind,ref,idem_key) VALUES ('<uid>',100000,(SELECT balance FROM users WHERE id='<uid>'),'admin_adjust','seed','seed-1');
```

Then: POST /v1/redemptions {"sku":"amazon_5"} → 201 pending; approve/deny via
`/admin/redemptions/...` with `X-Admin-Key`. Double-approve must 409.

## Final audit query (must all be true / >= 0)

```sql
SELECT u.id, u.balance = COALESCE(SUM(l.amount),0) AS consistent
FROM users u LEFT JOIN ledger l ON l.user_id=u.id GROUP BY u.id, u.balance;
SELECT MIN(balance_after) FROM ledger;
```

## Gotchas

- `$UID` is readonly in bash — name shell variables `USER_ID`.
- Happy hour defaults to 19–21 UTC; claims outside that window pay base.
- Auth rate limit is 5/min/IP — rapid probing trips 429s that bleed into
  later probes in the same minute window.
