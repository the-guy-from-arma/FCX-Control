# FCX Control

Standalone FCX / Ravenhood exchange API and FEC control PWA.

This service owns the shared exchange. Community CAD services are API clients; they never receive unrestricted access to the FCX PostgreSQL database and never communicate with one another.

## Service boundary

```text
CAD 1 (Faircroft) --authenticated API--> FCX Control
CAD 2             --authenticated API--> FCX Control

FCX Control --community settlement--> CAD 1 Bank Bridge
FCX Control --community settlement--> CAD 2 Bank Bridge
```

Each community has an independent credential, access policy, bank-bridge URL, and secret reference. A settlement is always bound to one community and one Ravenhood account.

## Included

- Shared FCX engine and exchange schema
- Ravenhood accounts and verified community links
- Community registry and per-community trading controls
- Hashed API credentials with rotation and revocation
- Idempotent settlement state machine
- FEC investigations and immutable audit events
- Server-side administrator RBAC and CSRF-protected sessions
- Standalone installable control PWA
- Railway/Docker deployment configuration

## Required Railway variables

Copy `.env.example` into Railway variables. Never commit live credentials.

`FCX_DATABASE_URL` must refer only to the dedicated FCX PostgreSQL service. It must never point to either CAD database.

## Local start

```bash
python -m pip install -r requirements.txt
uvicorn fcx_control.main:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080`. The first administrator is created only when both bootstrap variables are supplied and no administrator exists.

## Settlement safety

Settlement requests use a globally unique `fcx_txn_*` identifier plus a community-scoped idempotency key. State changes are monotonic and callbacks are checked against the originating community. Community bank balances remain authoritative.

## Production cutover

Do not point CAD 1 or CAD 2 at this service until database, community-authentication, bank-bridge, replay, and regression tests pass. The current monolith remains the rollback source during staged extraction.

