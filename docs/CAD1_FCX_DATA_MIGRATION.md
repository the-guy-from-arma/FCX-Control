# CAD 1 Ravenhood to FCX Database Migration

This runbook moves the complete Ravenhood/FCX data set out of the CAD 1
PostgreSQL database and into the dedicated FCX PostgreSQL database without
deleting or modifying the CAD 1 source rows.

## Ownership boundary

- CAD 1 keeps its residents, CAD/MDT, Arma linking, game bank, Bank Bridge,
  lottery, insurance, properties, and all other community-owned data.
- FCX owns Ravenhood accounts, holdings, orders, executions, margin positions,
  securities, companies/issuers, indexes, market/FEC history, automation state,
  and price history.
- CAD 1 and CAD 2 call FCX through authenticated community APIs. Neither CAD
  runtime receives `FCX_DATABASE_URL`.
- The one-time migration job is the only process that receives both database
  URLs. Remove its source credential after reconciliation.

## Data included

The migrator discovers and copies every `market_*`, `business_issuer_*`, and
`fcx_engine_*` table, plus the FCX/business market settings and the legacy
issuer archive. This includes resident brokerage cash, holdings, reserved
shares, cost basis, orders, queued orders, executions, transfers, margin
positions, liquidation records, FEC records, companies, capitalization
history, announcements, indexes, market-maker activity, events, and prices.

`bank_bridge_commands` is intentionally not moved. It remains in CAD 1 because
the game-bank bridge is community-owned. FCX stores settlement references and
calls the correct CAD bridge through its authenticated API.

## Identity preservation

CAD-local `users.id` values are not treated as global identities. The migration
creates a stable FCX Ravenhood account for every referenced CAD 1 user and a
`faircroft` community link. A verified Bohemia identity is carried into the
link when one exists. All migrated user references are rewritten to the new
central IDs, preventing orphaned cash, holdings, trades, issuer ownership, or
FEC history.

## Railway one-time job variables

Create a temporary migration service using this repository and set:

```env
SOURCE_CAD1_DATABASE_URL=${{CAD 1 DATABASE.DATABASE_URL}}
FCX_DATABASE_URL=${{FCX Database.DATABASE_URL}}
SOURCE_CAD1_COMMUNITY_ID=faircroft
FCX_RUN_SCHEDULER=0
```

Do not add `SOURCE_CAD1_DATABASE_URL` to the permanent FCX service or either
CAD service.

## Staged execution

1. Initialize and inspect only:

   ```text
   python scripts/migrate_cad1_ravenhood.py --phase plan --bootstrap-target --json
   ```

2. Confirm that the source and target database identities differ, all expected
   FCX tables are present in the plan, and the target has no unexpected live
   rows.

3. Copy and promote to FCX staging/operational tables:

   ```text
   python scripts/migrate_cad1_ravenhood.py --phase copy --bootstrap-target --json
   ```

4. Save the emitted `run_id`, then reconcile:

   ```text
   python scripts/migrate_cad1_ravenhood.py --phase verify --run-id RUN_ID --json
   ```

5. Only after reconciliation reports `ok: true`, mark the target cutover-ready:

   ```text
   python scripts/migrate_cad1_ravenhood.py --phase finalize --run-id RUN_ID --json
   ```

The `--allow-target-merge` switch is deliberately required when operational FCX
tables are already populated. Never use it without first reviewing the plan and
confirming those rows are intentional.

## Reconciliation gates

The migration will not authorize cutover unless:

- source, staged, and promoted row counts match for every included table;
- every mapped CAD user has a central Ravenhood account and Faircroft link;
- financial totals match for cash balances, holdings, order values and fees,
  collateral, margin P&L/payouts, transactions, issuer treasury/capitalization,
  and issuer ledgers;
- source and target are confirmed to be different PostgreSQL databases; and
- no migration phase wrote to the read-only CAD 1 transaction.

## Live-write cutover

The bulk copy is non-destructive, but CAD 1 can still receive Ravenhood writes
while it runs. Before production cutover:

1. Put only Ravenhood trading in CAD 1 into maintenance/read-only mode. Do not
   stop CAD, Arma linking, or the community Bank Bridge.
2. Run one final migration copy/verify cycle against the frozen Ravenhood data.
3. Confirm the reconciliation report again.
4. Start the permanent FCX service against `FCX_DATABASE_URL`.
5. Point CAD 1 player Ravenhood requests to the FCX API and test one read, buy,
   sell, margin open/close, and settlement callback.
6. Point CAD 2 to the same FCX API with its own community credential.
7. Keep the CAD 1 FCX tables unchanged as rollback evidence until acceptance is
   complete. They must not resume as an authoritative write path.
8. Delete the temporary migration service or remove its source database
   reference.

## Rollback

Before the final API cutover, rollback is simply to leave CAD 1 on its current
Ravenhood path; no source data was changed. After cutover, stop FCX writes,
retain the FCX audit trail, and follow a reviewed reverse-delta procedure rather
than overwriting either database.

