# FCX Autonomous Market Engine

This package adds an opt-in autonomous market layer to the existing Faircroft/Ravenhood application. It does not replace the PWA and it never writes resident cash, resident holdings, resident orders, Arma link records, or Bank Bridge commands.

## Operating boundary

- Existing `app.py` remains the PWA and resident-trading authority.
- Engine-owned persistence uses the `fcx_engine_*` table prefix.
- Ravenhood quote integration is limited to the established `market_securities`, `market_price_history`, and anonymous `market_system_trades` tape.
- PostgreSQL advisory locking prevents overlapping market ticks.
- The engine defaults to disabled. Enabling it from Dev Tools disables the legacy local/Gemini quote writers so two autonomous systems cannot race.
- The kill switch stops new autonomous work without taking the PWA or human trading offline.

## Integrated Railway mode

The standard web service starts a lightweight FCX worker from `app.py`. Open **Dev Tools > Stock Market** and use the **FCX Autonomous Exchange** console to configure, seed, test, and enable it.

Recommended rollout:

1. Leave the engine disabled.
2. Run the 1-, 7-, 30-, and 365-day sandboxes.
3. Seed the missing investor population.
4. Run individual manual cycles and inspect the cycle/audit ledgers.
5. Enable the engine at `LOW` speed.
6. Confirm capital conservation, price caps, flags, and PWA responsiveness before selecting `NORMAL`.

## Optional standalone service

The same engine can run independently with FastAPI and APScheduler:

```text
uvicorn fcx_engine.service:app --host 0.0.0.0 --port 8081
```

Required environment:

```text
FCX_DATABASE_URL=<dedicated FCX Railway PostgreSQL URL>
FCX_ENGINE_ADMIN_KEY=<separate strong service-administration secret>
```

Optional pool controls:

```text
FCX_DB_POOL_SIZE=2
FCX_DB_MAX_OVERFLOW=1
```

Do not run both the integrated worker and the standalone scheduler at the same time. If the standalone service is selected, leave `fcx_engine_enabled` configured centrally but disable the integrated worker at deployment level. Advisory locking prevents duplicate ticks, but one scheduler should own the clock operationally.

`FCX_DATABASE_URL` is intentionally strict. The standalone FCX service never
falls back to a CAD service's `DATABASE_URL`.

## Validation

```text
python -m unittest discover -s tests -p "test_fcx_engine.py"
python -m py_compile app.py fcx_engine/*.py
```

Sandbox simulation is deterministic for a supplied seed and cannot modify production records.
