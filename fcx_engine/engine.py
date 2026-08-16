from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
import time
from collections import defaultdict
from typing import Any

from .config import EngineConfig, PERSONALITY_PROFILES
from .personalities import decide
from .pricing import (
    discover_price,
    fear_greed,
    index_nav,
    ipo_uncertainty_multiplier,
    market_maker_quote,
    regime_for,
    short_squeeze_cover_quantity,
    split_adjustment,
)
from .sandbox import run_sandbox


ENGINE_LOCK_ID = 741629105
CYCLE_ORDER = ("minute", "five_minute", "fifteen_minute", "thirty_minute", "hourly", "six_hour", "daily")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat()


def _rows(db: Any, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(query, params).fetchall()]


def _one(db: Any, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = db.execute(query, params).fetchone()
    return dict(row) if row else None


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return fallback


def index_constituent_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Return index membership totals keyed by the public FCX ticker.

    ``market_index_funds.fund_key`` stores the internal identities
    ``stability`` and ``volatility``.  Readiness checks are expressed using
    the public tickers FCXS and FCXV, so using ``fund_key`` directly makes a
    populated index look empty.  Prefer the joined fund-security ticker and
    retain the aliases for callers working with older query shapes.
    """
    aliases = {"STABILITY": "FCXS", "VOLATILITY": "FCXV"}
    counts: dict[str, int] = {}
    for row in rows:
        identity = str(row.get("fund_ticker") or row.get("ticker") or row.get("fund_key") or "").strip().upper()
        identity = aliases.get(identity, identity)
        if identity:
            counts[identity] = int(row.get("constituents") or 0)
    return counts


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def ensure_schema(db: Any, timestamp: str | None = None) -> None:
    timestamp = timestamp or now_iso()
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_state (
            id INTEGER PRIMARY KEY CHECK (id=1),
            status TEXT NOT NULL DEFAULT 'offline',
            market_sentiment NUMERIC(8,4) NOT NULL DEFAULT 50,
            market_volatility NUMERIC(8,4) NOT NULL DEFAULT 35,
            fear_greed NUMERIC(8,4) NOT NULL DEFAULT 50,
            regime TEXT NOT NULL DEFAULT 'sideways',
            last_cycle_state_json TEXT NOT NULL DEFAULT '{}',
            last_heartbeat_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            seeded_at TEXT,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        "INSERT INTO fcx_engine_state (id,updated_at) VALUES (1,?) ON CONFLICT (id) DO NOTHING",
        (timestamp,),
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_npc_investors (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            personality TEXT NOT NULL,
            cash_balance NUMERIC(24,2) NOT NULL,
            initial_capital NUMERIC(24,2) NOT NULL,
            risk_tolerance NUMERIC(8,4) NOT NULL,
            confidence NUMERIC(8,4) NOT NULL,
            panic_threshold NUMERIC(8,4) NOT NULL,
            profit_target NUMERIC(8,4) NOT NULL,
            loss_threshold NUMERIC(8,4) NOT NULL,
            preferred_sectors_json TEXT NOT NULL DEFAULT '[]',
            max_position_percent NUMERIC(8,4) NOT NULL,
            min_cash_reserve_percent NUMERIC(8,4) NOT NULL,
            trade_frequency NUMERIC(8,4) NOT NULL,
            reaction_speed NUMERIC(8,4) NOT NULL,
            holding_period INTEGER NOT NULL,
            market_bias NUMERIC(8,4) NOT NULL DEFAULT 0,
            activity_level NUMERIC(8,4) NOT NULL DEFAULT 1,
            recent_wins INTEGER NOT NULL DEFAULT 0,
            recent_losses INTEGER NOT NULL DEFAULT 0,
            realized_pnl NUMERIC(24,2) NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            memory_json TEXT NOT NULL DEFAULT '{}',
            next_action_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_npc_due_idx ON fcx_engine_npc_investors(status,next_action_at)")
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_npc_personality_idx ON fcx_engine_npc_investors(personality,status)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_npc_positions (
            id SERIAL PRIMARY KEY,
            investor_id INTEGER NOT NULL,
            security_id INTEGER NOT NULL,
            quantity NUMERIC(24,8) NOT NULL DEFAULT 0,
            average_cost NUMERIC(18,4) NOT NULL DEFAULT 0,
            realized_pnl NUMERIC(24,2) NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(investor_id,security_id),
            FOREIGN KEY (investor_id) REFERENCES fcx_engine_npc_investors(id) ON DELETE CASCADE,
            FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_positions_security_idx ON fcx_engine_npc_positions(security_id,quantity)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_npc_shorts (
            id SERIAL PRIMARY KEY,
            investor_id INTEGER NOT NULL,
            security_id INTEGER NOT NULL,
            quantity NUMERIC(24,8) NOT NULL DEFAULT 0,
            average_entry NUMERIC(18,4) NOT NULL DEFAULT 0,
            collateral NUMERIC(24,2) NOT NULL DEFAULT 0,
            realized_pnl NUMERIC(24,2) NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(investor_id,security_id),
            FOREIGN KEY (investor_id) REFERENCES fcx_engine_npc_investors(id) ON DELETE CASCADE,
            FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_shorts_security_idx ON fcx_engine_npc_shorts(security_id,quantity)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_company_fundamentals (
            security_id INTEGER PRIMARY KEY,
            revenue NUMERIC(24,2) NOT NULL DEFAULT 0,
            expenses NUMERIC(24,2) NOT NULL DEFAULT 0,
            profit NUMERIC(24,2) NOT NULL DEFAULT 0,
            cash NUMERIC(24,2) NOT NULL DEFAULT 0,
            assets NUMERIC(24,2) NOT NULL DEFAULT 0,
            liabilities NUMERIC(24,2) NOT NULL DEFAULT 0,
            debt NUMERIC(24,2) NOT NULL DEFAULT 0,
            revenue_growth NUMERIC(10,4) NOT NULL DEFAULT 0,
            profit_growth NUMERIC(10,4) NOT NULL DEFAULT 0,
            cash_flow NUMERIC(24,2) NOT NULL DEFAULT 0,
            debt_ratio NUMERIC(10,4) NOT NULL DEFAULT 0,
            fair_value NUMERIC(18,4) NOT NULL DEFAULT 0,
            fundamental_score NUMERIC(8,4) NOT NULL DEFAULT 50,
            risk_score NUMERIC(8,4) NOT NULL DEFAULT 25,
            bankruptcy_risk NUMERIC(8,4) NOT NULL DEFAULT 0,
            company_sentiment NUMERIC(8,4) NOT NULL DEFAULT 50,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'healthy',
            analyzed_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_sector_state (
            sector TEXT PRIMARY KEY,
            sentiment NUMERIC(8,4) NOT NULL DEFAULT 50,
            performance NUMERIC(10,4) NOT NULL DEFAULT 0,
            volatility NUMERIC(8,4) NOT NULL DEFAULT 35,
            event_impact NUMERIC(10,4) NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_economic_events (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            affected_sectors_json TEXT NOT NULL DEFAULT '[]',
            sentiment_impact NUMERIC(10,4) NOT NULL DEFAULT 0,
            revenue_impact NUMERIC(10,4) NOT NULL DEFAULT 0,
            volatility_impact NUMERIC(10,4) NOT NULL DEFAULT 0,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            generated_by TEXT NOT NULL DEFAULT 'fcx_engine',
            created_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_events_status_idx ON fcx_engine_economic_events(status,ends_at)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_parent_orders (
            id SERIAL PRIMARY KEY,
            investor_id INTEGER NOT NULL,
            security_id INTEGER NOT NULL,
            side TEXT NOT NULL,
            total_quantity NUMERIC(24,8) NOT NULL,
            remaining_quantity NUMERIC(24,8) NOT NULL,
            child_size NUMERIC(24,8) NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            rationale TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (investor_id) REFERENCES fcx_engine_npc_investors(id) ON DELETE CASCADE,
            FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_parent_active_idx ON fcx_engine_parent_orders(status,updated_at)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_cycle_log (
            id SERIAL PRIMARY KEY,
            cycle_key TEXT NOT NULL,
            status TEXT NOT NULL,
            cycle_token TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            investors_evaluated INTEGER NOT NULL DEFAULT 0,
            trades_executed INTEGER NOT NULL DEFAULT 0,
            securities_moved INTEGER NOT NULL DEFAULT 0,
            volume NUMERIC(24,2) NOT NULL DEFAULT 0,
            summary_json TEXT NOT NULL DEFAULT '{}',
            error_message TEXT NOT NULL DEFAULT ''
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_cycles_time_idx ON fcx_engine_cycle_log(started_at DESC)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_audit_log (
            id SERIAL PRIMARY KEY,
            cycle_id INTEGER,
            investor_id INTEGER,
            personality TEXT NOT NULL DEFAULT '',
            security_id INTEGER,
            ticker TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            shares NUMERIC(24,8) NOT NULL DEFAULT 0,
            price NUMERIC(18,4) NOT NULL DEFAULT 0,
            notional NUMERIC(24,2) NOT NULL DEFAULT 0,
            reason_json TEXT NOT NULL DEFAULT '[]',
            confidence NUMERIC(8,4) NOT NULL DEFAULT 0,
            market_sentiment NUMERIC(8,4) NOT NULL DEFAULT 50,
            stock_sentiment NUMERIC(8,4) NOT NULL DEFAULT 50,
            risk_score NUMERIC(8,4) NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (cycle_id) REFERENCES fcx_engine_cycle_log(id) ON DELETE SET NULL,
            FOREIGN KEY (investor_id) REFERENCES fcx_engine_npc_investors(id) ON DELETE SET NULL,
            FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE SET NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_audit_time_idx ON fcx_engine_audit_log(created_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_audit_security_idx ON fcx_engine_audit_log(security_id,created_at DESC)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_risk_flags (
            id SERIAL PRIMARY KEY,
            flag_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            security_id INTEGER,
            user_id BIGINT,
            status TEXT NOT NULL DEFAULT 'open',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_at TEXT,
            FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE SET NULL,
            FOREIGN KEY (user_id) REFERENCES fcx_ravenhood_accounts(id) ON DELETE SET NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_flags_status_idx ON fcx_engine_risk_flags(status,last_seen_at DESC)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_liquidity_quotes (
            security_id INTEGER PRIMARY KEY,
            bid_price NUMERIC(18,4) NOT NULL,
            ask_price NUMERIC(18,4) NOT NULL,
            spread_percent NUMERIC(10,4) NOT NULL,
            bid_depth NUMERIC(24,8) NOT NULL DEFAULT 0,
            ask_depth NUMERIC(24,8) NOT NULL DEFAULT 0,
            provider_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_news (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            ticker TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT 'info',
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_corporate_actions (
            id SERIAL PRIMARY KEY,
            security_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            ratio_numerator NUMERIC(18,6) NOT NULL DEFAULT 1,
            ratio_denominator NUMERIC(18,6) NOT NULL DEFAULT 1,
            amount_per_share NUMERIC(24,4) NOT NULL DEFAULT 0,
            eligible_resident_shares NUMERIC(24,8) NOT NULL DEFAULT 0,
            eligible_npc_shares NUMERIC(24,8) NOT NULL DEFAULT 0,
            total_cash_amount NUMERIC(24,2) NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'completed',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_by BIGINT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE RESTRICT,
            FOREIGN KEY (created_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_corporate_security_idx ON fcx_engine_corporate_actions(security_id,created_at DESC)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_deployments (
            id SERIAL PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'running',
            target_listings INTEGER NOT NULL DEFAULT 30,
            listings_before INTEGER NOT NULL DEFAULT 0,
            listings_created INTEGER NOT NULL DEFAULT 0,
            listings_after INTEGER NOT NULL DEFAULT 0,
            archived_index_accounts INTEGER NOT NULL DEFAULT 0,
            archived_index_units NUMERIC(30,6) NOT NULL DEFAULT 0,
            index_constituents INTEGER NOT NULL DEFAULT 0,
            investors INTEGER NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}',
            deployed_by BIGINT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (deployed_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_deployments_time_idx ON fcx_engine_deployments(created_at DESC)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS fcx_engine_index_unit_archive (
            id SERIAL PRIMARY KEY,
            deployment_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            security_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            quantity NUMERIC(30,6) NOT NULL,
            average_cost NUMERIC(18,4) NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}',
            archived_at TEXT NOT NULL,
            FOREIGN KEY (deployment_id) REFERENCES fcx_engine_deployments(id) ON DELETE CASCADE
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS fcx_engine_index_archive_deployment_idx ON fcx_engine_index_unit_archive(deployment_id,account_id)")
    # The PWA owns this established table. These additive fields distinguish
    # temporary engine circuit breakers from manual FEC/developer halts.
    db.execute("ALTER TABLE market_security_halts ADD COLUMN IF NOT EXISTS automatic_resume_at TEXT")
    db.execute("ALTER TABLE market_security_halts ADD COLUMN IF NOT EXISTS engine_managed INTEGER NOT NULL DEFAULT 0")


def seed_investors(db: Any, settings: dict[str, Any], replace: bool = False) -> dict[str, Any]:
    config = EngineConfig.from_settings(settings)
    current = int((_one(db, "SELECT COUNT(*) AS count FROM fcx_engine_npc_investors") or {}).get("count") or 0)
    if replace:
        db.execute("DELETE FROM fcx_engine_npc_investors")
        current = 0
    required = max(0, config.population - current)
    if required <= 0:
        return {"created": 0, "population": current, "capital": _capital_stats(db)}
    rng = random.Random(config.random_seed + current)
    weighted: list[str] = []
    for personality, weight in config.distribution.items():
        weighted.extend([personality] * max(1, int(round(weight * 10))))
    sectors = [str(row.get("sector") or "General") for row in _rows(db, "SELECT DISTINCT sector FROM market_securities WHERE active=1 ORDER BY sector")]
    if not sectors:
        sectors = ["General"]
    existing_capital = float((_one(db, "SELECT COALESCE(SUM(initial_capital),0) AS total FROM fcx_engine_npc_investors") or {}).get("total") or 0)
    capital_to_allocate = max(1000.0 * required, config.total_capital - existing_capital)
    raw_weights = [math.exp(rng.gauss(0, 1.15)) for _ in range(required)]
    scale = capital_to_allocate / max(sum(raw_weights), 1.0)
    timestamp = now_iso()
    inserts: list[tuple[Any, ...]] = []
    for offset in range(required):
        personality = rng.choice(weighted)
        profile = PERSONALITY_PROFILES[personality]
        capital = max(1000.0, raw_weights[offset] * scale)
        risk = max(1.0, min(99.0, profile["risk"] + rng.uniform(-12, 12)))
        confidence = max(5.0, min(95.0, rng.gauss(54, 18)))
        preferred = rng.sample(sectors, min(len(sectors), rng.randint(1, min(3, len(sectors)))))
        name = f"FCX-{personality[:4].upper()}-{current + offset + 1:05d}"
        inserts.append((
            name, personality, round(capital, 2), round(capital, 2), round(risk, 2), round(confidence, 2),
            round(rng.uniform(15, 55), 2), round(rng.uniform(8, 40), 2), round(rng.uniform(5, 30), 2),
            json.dumps(preferred, separators=(",", ":")), round(profile["size"] * 100, 3),
            round(profile["reserve"] * 100, 3), round(profile["frequency"], 4), round(rng.uniform(0.3, 1.0), 4),
            rng.randint(1, 240), round(rng.uniform(-0.35, 0.35), 4), round(rng.uniform(0.7, 1.3), 4),
            timestamp, timestamp, timestamp,
        ))
    db.executemany(
        """INSERT INTO fcx_engine_npc_investors
           (name,personality,cash_balance,initial_capital,risk_tolerance,confidence,panic_threshold,
            profit_target,loss_threshold,preferred_sectors_json,max_position_percent,min_cash_reserve_percent,
            trade_frequency,reaction_speed,holding_period,market_bias,activity_level,next_action_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        inserts,
    )
    db.execute("UPDATE fcx_engine_state SET seeded_at=COALESCE(seeded_at,?),updated_at=? WHERE id=1", (timestamp, timestamp))
    return {"created": required, "population": current + required, "capital": _capital_stats(db)}


def _capital_stats(db: Any) -> dict[str, float]:
    row = _one(
        db,
        """SELECT COALESCE(SUM(initial_capital),0) AS initial_capital,
                  COALESCE(SUM(cash_balance),0) AS cash,
                  COALESCE(SUM(realized_pnl),0) AS realized_pnl
           FROM fcx_engine_npc_investors""",
    ) or {}
    positions = _one(
        db,
        """SELECT COALESCE(SUM(p.quantity*s.price),0) AS positions
           FROM fcx_engine_npc_positions p JOIN market_securities s ON s.id=p.security_id
           WHERE p.quantity>0""",
    ) or {}
    shorts = _one(
        db,
        """SELECT COALESCE(SUM(collateral),0) AS collateral,
                  COALESCE(SUM((average_entry-s.price)*sh.quantity),0) AS unrealized
           FROM fcx_engine_npc_shorts sh JOIN market_securities s ON s.id=sh.security_id
           WHERE sh.quantity>0""",
    ) or {}
    initial = float(row.get("initial_capital") or 0)
    cash = float(row.get("cash") or 0)
    position_value = float(positions.get("positions") or 0)
    short_collateral = float(shorts.get("collateral") or 0)
    short_unrealized = float(shorts.get("unrealized") or 0)
    return {
        "initial_capital": round(initial, 2),
        "cash": round(cash, 2),
        "positions": round(position_value, 2),
        "short_collateral": round(short_collateral, 2),
        "short_unrealized": round(short_unrealized, 2),
        "equity": round(cash + position_value + short_collateral + short_unrealized, 2),
        "realized_pnl": round(float(row.get("realized_pnl") or 0), 2),
        "inflation_percent": round(((cash + position_value + short_collateral + short_unrealized) / initial - 1) * 100, 4) if initial else 0.0,
    }


def _event_effects(db: Any, securities: list[dict[str, Any]], timestamp: str) -> tuple[dict[int, dict[str, float]], dict[str, float]]:
    db.execute("UPDATE fcx_engine_economic_events SET status='expired' WHERE status='active' AND ends_at<=?", (timestamp,))
    active = _rows(db, "SELECT * FROM fcx_engine_economic_events WHERE status='active' AND starts_at<=? AND ends_at>?", (timestamp, timestamp))
    sector_rows = _rows(db, "SELECT * FROM fcx_engine_sector_state")
    sectors = {str(row.get("sector") or "General"): row for row in sector_rows}
    per_security: dict[int, dict[str, float]] = {}
    market = {"sentiment": 0.0, "volatility": 0.0, "revenue": 0.0, "events": float(len(active))}
    for event in active:
        affected = {str(value) for value in _json(event.get("affected_sectors_json"), [])}
        market["sentiment"] += float(event.get("sentiment_impact") or 0) * 0.25
        market["volatility"] += float(event.get("volatility_impact") or 0) * 0.25
        market["revenue"] += float(event.get("revenue_impact") or 0) * 0.25
        for security in securities:
            sector = str(security.get("sector") or "General")
            if affected and sector not in affected and "All" not in affected and "General" not in affected:
                continue
            bucket = per_security.setdefault(int(security["id"]), {"sentiment": 0.0, "volatility": 0.0, "revenue": 0.0})
            bucket["sentiment"] += float(event.get("sentiment_impact") or 0)
            bucket["volatility"] += float(event.get("volatility_impact") or 0)
            bucket["revenue"] += float(event.get("revenue_impact") or 0)
    for security in securities:
        bucket = per_security.setdefault(int(security["id"]), {"sentiment": 0.0, "volatility": 0.0, "revenue": 0.0})
        sector_state = sectors.get(str(security.get("sector") or "General")) or {}
        bucket["sentiment"] += (float(sector_state.get("sentiment") or 50) - 50) * 0.35
        bucket["volatility"] += max(0.0, float(sector_state.get("volatility") or 35) - 35) * 0.20
    return per_security, market


def _execute_parent_order_children(
    db: Any,
    config: EngineConfig,
    cycle_id: int,
    securities: list[dict[str, Any]],
    timestamp: str,
) -> tuple[dict[int, dict[str, float]], list[dict[str, Any]]]:
    available_securities = {int(row["id"]): row for row in securities}
    orders = _rows(
        db,
        """SELECT po.*,i.cash_balance,i.initial_capital,i.min_cash_reserve_percent,i.personality
           FROM fcx_engine_parent_orders po JOIN fcx_engine_npc_investors i ON i.id=po.investor_id
           WHERE po.status='active' AND po.remaining_quantity>0 AND i.status='active'
           ORDER BY po.id LIMIT 25 FOR UPDATE SKIP LOCKED""",
    )
    flow: dict[int, dict[str, float]] = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "buy_count": 0.0, "sell_count": 0.0})
    fills: list[dict[str, Any]] = []
    for order in orders:
        security = available_securities.get(int(order["security_id"]))
        if not security:
            continue
        side = str(order.get("side") or "buy").strip().lower()
        if side not in {"buy", "sell"}:
            db.execute("UPDATE fcx_engine_parent_orders SET status='cancelled',rationale=rationale || ' Unsupported side.',updated_at=? WHERE id=?", (timestamp, order["id"]))
            continue
        price = max(config.price_floor, float(security.get("price") or config.price_floor))
        investor = _one(db, "SELECT * FROM fcx_engine_npc_investors WHERE id=? FOR UPDATE", (order["investor_id"],)) or order
        cash = max(0.0, float(investor.get("cash_balance") or 0))
        position = _one(db, "SELECT * FROM fcx_engine_npc_positions WHERE investor_id=? AND security_id=? FOR UPDATE", (order["investor_id"], order["security_id"])) or {}
        held = max(0.0, float(position.get("quantity") or 0))
        if side == "buy":
            reserve = float(investor.get("initial_capital") or cash) * float(investor.get("min_cash_reserve_percent") or 20) / 100
            available = max(0.0, cash - reserve)
            capacity = available / price
        else:
            capacity = held
        quantity = min(float(order.get("remaining_quantity") or 0), float(order.get("child_size") or 0), capacity)
        if quantity <= 0:
            reason = "Insufficient deployable capital." if side == "buy" else "No remaining inventory to liquidate."
            db.execute("UPDATE fcx_engine_parent_orders SET status='cancelled',rationale=rationale || ?,updated_at=? WHERE id=?", (f" {reason}", timestamp, order["id"]))
            continue
        quantity = round(quantity, 6)
        notional = round(quantity * price, 2)
        average = float(position.get("average_cost") or price)
        realized = 0.0
        if side == "buy":
            new_quantity = held + quantity
            new_average = ((held * average) + notional) / max(new_quantity, 0.000001)
            db.execute(
                """INSERT INTO fcx_engine_npc_positions (investor_id,security_id,quantity,average_cost,opened_at,updated_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT (investor_id,security_id) DO UPDATE SET
                   quantity=excluded.quantity,average_cost=excluded.average_cost,updated_at=excluded.updated_at""",
                (order["investor_id"], order["security_id"], new_quantity, new_average, timestamp, timestamp),
            )
            db.execute("UPDATE fcx_engine_npc_investors SET cash_balance=cash_balance-?,updated_at=? WHERE id=?", (notional, timestamp, order["investor_id"]))
        else:
            new_quantity = max(0.0, held - quantity)
            realized = round((price - average) * quantity, 2)
            db.execute(
                """UPDATE fcx_engine_npc_positions SET quantity=?,realized_pnl=realized_pnl+?,updated_at=?
                   WHERE investor_id=? AND security_id=?""",
                (new_quantity, realized, timestamp, order["investor_id"], order["security_id"]),
            )
            db.execute(
                """UPDATE fcx_engine_npc_investors SET cash_balance=cash_balance+?,realized_pnl=realized_pnl+?,
                   recent_wins=recent_wins+?,recent_losses=recent_losses+?,updated_at=? WHERE id=?""",
                (notional, realized, 1 if realized > 0 else 0, 1 if realized < 0 else 0, timestamp, order["investor_id"]),
            )
        remaining = max(0.0, float(order["remaining_quantity"]) - quantity)
        db.execute("UPDATE fcx_engine_parent_orders SET remaining_quantity=?,status=?,updated_at=? WHERE id=?", (remaining, "filled" if remaining <= 0.000001 else "active", timestamp, order["id"]))
        flow[int(order["security_id"])][side] += quantity
        flow[int(order["security_id"])][f"{side}_count"] += 1
        fills.append({"order": order, "security": security, "side": side, "quantity": quantity, "price": price, "notional": notional, "realized_pnl": realized})
        db.execute(
            """INSERT INTO fcx_engine_audit_log
               (cycle_id,investor_id,personality,security_id,ticker,action,shares,price,notional,reason_json,
                confidence,market_sentiment,stock_sentiment,risk_score,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,90,50,50,0,?)""",
            (cycle_id, order["investor_id"], order["personality"], order["security_id"], security["ticker"],
             f"PARENT_CHILD_{side.upper()}", quantity, price, notional,
             json.dumps([order.get("rationale") or "Parent order child execution"]), timestamp),
        )
    return flow, fills


def _apply_short_squeeze_covers(
    db: Any,
    config: EngineConfig,
    cycle_id: int,
    securities: list[dict[str, Any]],
    timestamp: str,
) -> tuple[dict[int, dict[str, float]], list[dict[str, Any]]]:
    """Force a bounded portion of crowded shorts to cover into a rally."""
    flow: dict[int, dict[str, float]] = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "buy_count": 0.0, "sell_count": 0.0})
    fills: list[dict[str, Any]] = []
    totals = {
        int(row["security_id"]): float(row.get("quantity") or 0)
        for row in _rows(db, "SELECT security_id,COALESCE(SUM(quantity),0) AS quantity FROM fcx_engine_npc_shorts WHERE quantity>0 GROUP BY security_id")
    }
    for security in securities:
        security_id = int(security["id"])
        short_quantity = totals.get(security_id, 0.0)
        current_price = max(config.price_floor, float(security.get("price") or config.price_floor))
        previous_price = max(config.price_floor, float(security.get("previous_price") or current_price))
        momentum = (current_price / previous_price - 1.0) * 100.0
        forced_quantity = short_squeeze_cover_quantity(
            short_quantity,
            float(security.get("issued_shares") or 1_000_000),
            momentum,
            float(security.get("volatility") or 35),
        )
        if forced_quantity <= 0:
            continue
        remaining = forced_quantity
        short_positions = _rows(
            db,
            """SELECT sh.*,i.personality FROM fcx_engine_npc_shorts sh
               JOIN fcx_engine_npc_investors i ON i.id=sh.investor_id
               WHERE sh.security_id=? AND sh.quantity>0 AND i.status='active'
               ORDER BY sh.average_entry ASC,sh.id FOR UPDATE OF sh SKIP LOCKED""",
            (security_id,),
        )
        for short in short_positions:
            if remaining <= 0.000001:
                break
            open_quantity = max(0.0, float(short.get("quantity") or 0))
            quantity = round(min(open_quantity, remaining), 6)
            if quantity <= 0:
                continue
            average_entry = max(config.price_floor, float(short.get("average_entry") or current_price))
            collateral = max(0.0, float(short.get("collateral") or 0))
            released = collateral * quantity / max(open_quantity, 0.000001)
            realized = round((average_entry - current_price) * quantity, 2)
            db.execute(
                """UPDATE fcx_engine_npc_shorts SET quantity=?,collateral=?,realized_pnl=realized_pnl+?,updated_at=?
                   WHERE id=?""",
                (max(0.0, open_quantity - quantity), max(0.0, collateral - released), realized, timestamp, short["id"]),
            )
            db.execute(
                """UPDATE fcx_engine_npc_investors SET cash_balance=GREATEST(0,cash_balance+?),
                   realized_pnl=realized_pnl+?,recent_wins=recent_wins+?,recent_losses=recent_losses+?,updated_at=?
                   WHERE id=?""",
                (released + realized, realized, 1 if realized > 0 else 0, 1 if realized < 0 else 0, timestamp, short["investor_id"]),
            )
            flow[security_id]["buy"] += quantity
            flow[security_id]["buy_count"] += 1
            remaining -= quantity
            fill = {"investor_id": short["investor_id"], "security_id": security_id, "ticker": security["ticker"], "quantity": quantity, "price": current_price, "realized_pnl": realized}
            fills.append(fill)
            db.execute(
                """INSERT INTO fcx_engine_audit_log
                   (cycle_id,investor_id,personality,security_id,ticker,action,shares,price,notional,reason_json,
                    confidence,market_sentiment,stock_sentiment,risk_score,created_at)
                   VALUES (?,?,?,?,?,'SHORT_SQUEEZE_COVER',?,?,?,?,95,50,50,0,?)""",
                (cycle_id, short["investor_id"], short["personality"], security_id, security["ticker"], quantity,
                 current_price, round(quantity * current_price, 2),
                 json.dumps([f"Short interest and {momentum:+.2f}% momentum triggered a bounded forced cover"]), timestamp),
            )
    return flow, fills


def _load_securities(db: Any) -> list[dict[str, Any]]:
    return _rows(
        db,
        """SELECT s.*,f.security_id AS engine_fundamental_id,COALESCE(f.fundamental_score,50) AS fundamental_score,
                  COALESCE(f.risk_score,25) AS risk_score,COALESCE(f.bankruptcy_risk,0) AS bankruptcy_risk,
                  COALESCE(f.company_sentiment,50) AS company_sentiment,
                  COALESCE(NULLIF(f.fair_value,0),s.price) AS fair_value,
                  issuer.activated_at AS ipo_activated_at
           FROM market_securities s
           LEFT JOIN fcx_engine_company_fundamentals f ON f.security_id=s.id
           LEFT JOIN business_issuer_companies issuer ON issuer.security_id=s.id AND issuer.status='active'
           WHERE s.active=1 AND COALESCE(s.lifecycle_status,'active')='active'
             AND COALESCE(s.security_type,'stock')<>'fund'
             AND NOT EXISTS (SELECT 1 FROM market_security_halts h WHERE h.security_id=s.id AND h.status='active')
             AND NOT EXISTS (SELECT 1 FROM market_security_delistings d WHERE d.security_id=s.id AND d.status='active')
           ORDER BY s.id""",
    )


def _apply_price_programs(db: Any, config: EngineConfig, cycle_id: int, timestamp: str) -> dict[str, Any]:
    """Advance FEC price programs into Ravenhood's authoritative quote stream.

    The operations API stores scheduled movements in ``market_price_programs``.
    Ravenhood, however, renders ``market_securities`` and
    ``market_price_history``.  Keeping this bridge in the minute engine cycle
    makes a program auditable and prevents the autonomous discovery pass from
    being a separate, invisible market.
    """
    current_time = _parse_time(timestamp) or utcnow()
    programs = _rows(
        db,
        """SELECT p.*,s.ticker,s.name,s.price AS live_price,s.issued_shares,
                  COALESCE(s.security_type,'stock') AS security_type
           FROM market_price_programs p
           JOIN market_securities s ON s.id=p.security_id
           WHERE p.status IN ('scheduled','active')
             AND s.active=1 AND COALESCE(s.lifecycle_status,'active')='active'
           ORDER BY p.security_id,p.id""",
    )
    # Only the newest simultaneously-active instruction controls a security.
    selected: dict[int, dict[str, Any]] = {}
    for program in programs:
        starts_at = _parse_time(program.get("starts_at"))
        if starts_at and starts_at <= current_time:
            selected[int(program["security_id"])] = program

    moved = 0
    activated = 0
    completed = 0
    tickers: list[str] = []
    for security_id, program in selected.items():
        starts_at = _parse_time(program.get("starts_at")) or current_time
        ends_at = _parse_time(program.get("ends_at")) or starts_at
        live_price = max(config.price_floor, float(program.get("live_price") or config.price_floor))
        start_price = max(config.price_floor, float(program.get("start_price") or live_price))
        percent_change = float(program.get("percent_change") or 0)

        if str(program.get("status") or "") == "scheduled":
            # A future instruction locks its opening quote when it actually
            # begins, not when an investigator created it.
            start_price = live_price
            target_price = max(config.price_floor, round(start_price * (1 + percent_change / 100.0), 4))
            db.execute(
                "UPDATE market_price_programs SET status='active',start_price=?,target_price=? WHERE id=?",
                (start_price, target_price, program["id"]),
            )
            activated += 1
        else:
            target_price = max(config.price_floor, float(program.get("target_price") or start_price))

        duration = max(1.0, (ends_at - starts_at).total_seconds())
        progress = max(0.0, min(1.0, (current_time - starts_at).total_seconds() / duration))
        # Smoothstep avoids an artificial vertical jump while still arriving
        # at the exact ordered closing quote.
        eased = progress * progress * (3.0 - 2.0 * progress)
        new_price = max(config.price_floor, round(start_price + (target_price - start_price) * eased, 4))
        changed = abs(new_price - live_price) >= 0.00005
        if changed:
            movement = ((new_price / live_price) - 1.0) * 100.0 if live_price else 0.0
            issued = max(1.0, float(program.get("issued_shares") or 1_000_000))
            volume = round(max(1.0, min(issued * 0.0025, issued * max(0.00001, abs(movement) * 0.0001))), 6)
            is_up = new_price >= live_price
            db.execute(
                "UPDATE market_securities SET previous_price=price,price=?,updated_at=? WHERE id=?",
                (new_price, timestamp, security_id),
            )
            db.execute(
                "INSERT INTO market_price_history (security_id,price,source,recorded_at) VALUES (?,?,'fec_price_program',?)",
                (security_id, new_price, timestamp),
            )
            db.execute(
                """INSERT INTO market_system_trades
                   (security_id,buy_volume,sell_volume,buy_trade_count,sell_trade_count,reference_price,
                    price_change_percent,source,rationale,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (security_id, volume if is_up else 0, 0 if is_up else volume, 1 if is_up else 0,
                 0 if is_up else 1, new_price, round(movement, 4), "fec_price_program",
                 str(program.get("event_name") or "FEC scheduled price program")[:300], timestamp),
            )
            db.execute(
                """INSERT INTO fcx_engine_audit_log
                   (cycle_id,security_id,ticker,action,shares,price,notional,reason_json,confidence,
                    market_sentiment,stock_sentiment,risk_score,created_at)
                   VALUES (?,?,?,'FEC_PRICE_PROGRAM',?,?,0,?,100,50,50,0,?)""",
                (cycle_id, security_id, program.get("ticker"), volume, new_price,
                 json.dumps([str(program.get("event_name") or "FEC scheduled price program")], separators=(",", ":")), timestamp),
            )
            moved += 1
            tickers.append(str(program.get("ticker") or security_id))

        if progress >= 1.0:
            # Index funds normally revalue from their constituents.  Rebase the
            # NAV so a completed FEC instruction remains where it closed rather
            # than snapping back on the next engine tick.
            fund = _one(db, "SELECT id,base_nav FROM market_index_funds WHERE security_id=?", (security_id,))
            if fund:
                members = _rows(
                    db,
                    """SELECT m.weight,m.reference_price,s.price FROM market_index_members m
                       JOIN market_securities s ON s.id=m.security_id WHERE m.fund_id=?""",
                    (fund["id"],),
                )
                calculated = index_nav(
                    float(fund.get("base_nav") or 100),
                    [(float(item.get("weight") or 0), float(item.get("price") or 0),
                      float(item.get("reference_price") or 0)) for item in members],
                    config.price_floor,
                ) if members else target_price
                rebased = float(fund.get("base_nav") or 100) * target_price / max(config.price_floor, calculated)
                db.execute("UPDATE market_index_funds SET base_nav=?,last_valued_at=? WHERE id=?", (rebased, timestamp, fund["id"]))
            db.execute("UPDATE market_price_programs SET status='completed',target_price=?,ends_at=? WHERE id=?", (target_price, timestamp, program["id"]))
            completed += 1

    return {"active": len(selected), "activated": activated, "moved": moved, "completed": completed, "tickers": tickers}


def _corporate_security(db: Any, ticker: str) -> dict[str, Any]:
    row = _one(
        db,
        """SELECT * FROM market_securities
           WHERE UPPER(ticker)=UPPER(?) AND active=1
             AND COALESCE(lifecycle_status,'active')='active'
             AND COALESCE(security_type,'stock')<>'fund' LIMIT 1""",
        (str(ticker or "").strip(),),
    )
    if not row:
        raise ValueError("Select an active operating-company ticker")
    return row


def apply_stock_split(
    db: Any,
    ticker: str,
    numerator: float,
    denominator: float,
    actor_id: int | None = None,
    rationale: str = "",
) -> dict[str, Any]:
    """Apply an audited forward or reverse split across every FCX ledger.

    Market value, cash, realized P&L, and order notionals are preserved. Only
    share quantities and per-share prices change. Historical quotes remain
    immutable so the chart can explain the corporate action.
    """
    security = _corporate_security(db, ticker)
    numerator = float(numerator)
    denominator = float(denominator)
    if not 0.01 <= numerator <= 1000 or not 0.01 <= denominator <= 1000:
        raise ValueError("Split numerator and denominator must be between 0.01 and 1,000")
    ratio = numerator / denominator
    if not 0.01 <= ratio <= 100:
        raise ValueError("The resulting split ratio must be between 1:100 and 100:1")
    timestamp = now_iso()
    new_issued, new_price = split_adjustment(
        float(security.get("issued_shares") or 1_000_000),
        float(security.get("price") or 0), numerator, denominator,
    )
    _, new_previous = split_adjustment(0, float(security.get("previous_price") or security.get("price") or 0), numerator, denominator)
    security_id = int(security["id"])
    db.execute(
        "UPDATE market_securities SET issued_shares=?,price=?,previous_price=?,updated_at=? WHERE id=?",
        (new_issued, new_price, new_previous, timestamp, security_id),
    )
    db.execute("UPDATE market_holdings SET quantity=quantity*?,average_cost=average_cost/? WHERE security_id=?", (ratio, ratio, security_id))
    db.execute("UPDATE fcx_engine_npc_positions SET quantity=quantity*?,average_cost=average_cost/?,updated_at=? WHERE security_id=?", (ratio, ratio, timestamp, security_id))
    db.execute("UPDATE fcx_engine_npc_shorts SET quantity=quantity*?,average_entry=average_entry/?,updated_at=? WHERE security_id=?", (ratio, ratio, timestamp, security_id))
    db.execute(
        """UPDATE market_margin_positions SET quantity=quantity*?,entry_price=entry_price/?,
                  liquidation_price=liquidation_price/?,close_price=CASE WHEN close_price IS NULL THEN NULL ELSE close_price/? END
           WHERE security_id=?""",
        (ratio, ratio, ratio, ratio, security_id),
    )
    db.execute(
        """UPDATE market_order_requests SET quantity=quantity*?,submitted_price=submitted_price/?,
                  executed_unit_price=CASE WHEN executed_unit_price IS NULL THEN NULL ELSE executed_unit_price/? END
           WHERE security_id=? AND status IN ('queued','processing')""",
        (ratio, ratio, ratio, security_id),
    )
    db.execute(
        """UPDATE market_margin_order_requests SET submitted_price=submitted_price/?,
                  estimated_liquidation_price=estimated_liquidation_price/?
           WHERE security_id=? AND status IN ('queued','processing')""",
        (ratio, ratio, security_id),
    )
    db.execute(
        """UPDATE fcx_engine_parent_orders SET total_quantity=total_quantity*?,
                  remaining_quantity=remaining_quantity*?,child_size=child_size*?,updated_at=?
           WHERE security_id=? AND status='active'""",
        (ratio, ratio, ratio, timestamp, security_id),
    )
    db.execute("UPDATE fcx_engine_company_fundamentals SET fair_value=fair_value/?,updated_at=? WHERE security_id=?", (ratio, timestamp, security_id))
    db.execute("UPDATE market_index_members SET reference_price=reference_price/? WHERE security_id=?", (ratio, security_id))
    db.execute(
        """UPDATE business_issuer_companies SET opening_share_price=opening_share_price/?,
                  authorized_shares=authorized_shares*?,founder_shares=founder_shares*?,
                  issuer_inventory=issuer_inventory*?,updated_at=? WHERE security_id=?""",
        (ratio, ratio, ratio, ratio, timestamp, security_id),
    )
    details = {
        "ticker": security["ticker"], "ratio": f"{numerator:g}:{denominator:g}",
        "old_price": float(security.get("price") or 0), "new_price": new_price,
        "market_value_preserved": True, "rationale": str(rationale or "")[:1000],
    }
    db.execute(
        """INSERT INTO fcx_engine_corporate_actions
           (security_id,action_type,ratio_numerator,ratio_denominator,details_json,created_by,created_at)
           VALUES (?,'stock_split',?,?,?,?,?)""",
        (security_id, numerator, denominator, json.dumps(details, separators=(",", ":")), actor_id, timestamp),
    )
    db.execute("INSERT INTO market_price_history (security_id,price,source,recorded_at) VALUES (?,?,'fcx_stock_split',?)", (security_id, new_price, timestamp))
    db.execute(
        """INSERT INTO fcx_engine_audit_log
           (security_id,ticker,action,price,reason_json,confidence,created_at)
           VALUES (?,?,'STOCK_SPLIT',?,?,100,?)""",
        (security_id, security["ticker"], new_price, json.dumps(details, separators=(",", ":")), timestamp),
    )
    db.execute(
        "INSERT INTO fcx_engine_news (event_type,ticker,severity,reason,payload_json,created_at) VALUES ('stock_split',?,'info',?,?,?)",
        (security["ticker"], f"{security['ticker']} completed a {numerator:g}-for-{denominator:g} stock split.", json.dumps(details, separators=(",", ":")), timestamp),
    )
    return {"ok": True, **details, "issued_shares": new_issued}


def apply_dividend(
    db: Any,
    ticker: str,
    amount_per_share: float,
    actor_id: int | None = None,
    rationale: str = "",
) -> dict[str, Any]:
    """Pay a cash dividend from company fundamentals to long shareholders."""
    security = _corporate_security(db, ticker)
    amount = round(float(amount_per_share), 4)
    if not 0.0001 <= amount <= 1_000_000:
        raise ValueError("Dividend per share must be between 0.0001 and 1,000,000")
    security_id = int(security["id"])
    resident_shares = float((_one(db, "SELECT COALESCE(SUM(quantity),0) AS shares FROM market_holdings WHERE security_id=? AND quantity>0", (security_id,)) or {}).get("shares") or 0)
    npc_shares = float((_one(db, "SELECT COALESCE(SUM(quantity),0) AS shares FROM fcx_engine_npc_positions WHERE security_id=? AND quantity>0", (security_id,)) or {}).get("shares") or 0)
    total = round((resident_shares + npc_shares) * amount, 2)
    fundamentals = _one(db, "SELECT cash FROM fcx_engine_company_fundamentals WHERE security_id=? FOR UPDATE", (security_id,))
    if not fundamentals:
        raise ValueError("Company fundamentals are not initialized; run a minute cycle first")
    if total <= 0:
        raise ValueError("This company has no eligible long shareholders")
    if float(fundamentals.get("cash") or 0) + 0.0001 < total:
        raise ValueError(f"Company cash cannot cover the {total:,.2f} dividend obligation")
    timestamp = now_iso()
    db.execute(
        """UPDATE market_accounts a SET cash_balance=a.cash_balance+x.payout,updated_at=?
           FROM (SELECT account_id,SUM(quantity)*? AS payout FROM market_holdings
                 WHERE security_id=? AND quantity>0 GROUP BY account_id) x WHERE a.id=x.account_id""",
        (timestamp, amount, security_id),
    )
    db.execute(
        """UPDATE fcx_engine_npc_investors i SET cash_balance=i.cash_balance+x.payout,updated_at=?
           FROM (SELECT investor_id,SUM(quantity)*? AS payout FROM fcx_engine_npc_positions
                 WHERE security_id=? AND quantity>0 GROUP BY investor_id) x WHERE i.id=x.investor_id""",
        (timestamp, amount, security_id),
    )
    db.execute("UPDATE fcx_engine_company_fundamentals SET cash=cash-?,cash_flow=cash_flow-?,updated_at=? WHERE security_id=?", (total, total, timestamp, security_id))
    details = {
        "ticker": security["ticker"], "amount_per_share": amount,
        "resident_shares": resident_shares, "npc_shares": npc_shares,
        "total_cash_amount": total, "rationale": str(rationale or "")[:1000],
    }
    db.execute(
        """INSERT INTO fcx_engine_corporate_actions
           (security_id,action_type,amount_per_share,eligible_resident_shares,eligible_npc_shares,
            total_cash_amount,details_json,created_by,created_at)
           VALUES (?,'cash_dividend',?,?,?,?,?,?,?)""",
        (security_id, amount, resident_shares, npc_shares, total, json.dumps(details, separators=(",", ":")), actor_id, timestamp),
    )
    db.execute(
        """INSERT INTO fcx_engine_audit_log
           (security_id,ticker,action,notional,reason_json,confidence,created_at)
           VALUES (?,?,'CASH_DIVIDEND',?,?,100,?)""",
        (security_id, security["ticker"], total, json.dumps(details, separators=(",", ":")), timestamp),
    )
    db.execute(
        "INSERT INTO fcx_engine_news (event_type,ticker,severity,reason,payload_json,created_at) VALUES ('cash_dividend',?,'info',?,?,?)",
        (security["ticker"], f"{security['ticker']} declared a {amount:,.4f} FC$ cash dividend per share.", json.dumps(details, separators=(",", ":")), timestamp),
    )
    return {"ok": True, **details}


def _revalue_index_funds(db: Any, config: EngineConfig, cycle_id: int, timestamp: str) -> int:
    """Revalue FCXS/FCXV from constituents without trading resident fund units.

    The autonomous engine owns the movement of operating listings only.  Index
    securities remain compatible with Ravenhood's existing fund tables and are
    repriced from their configured weights/reference prices after each minute
    cycle.  This prevents an index from being treated like an independent NPC
    stock while preserving every resident holding and order contract.
    """
    funds = _rows(
        db,
        """SELECT f.id,f.fund_key,f.security_id,f.base_nav,f.last_valued_at,
                  s.ticker,s.price,s.previous_price
           FROM market_index_funds f JOIN market_securities s ON s.id=f.security_id
           WHERE f.enabled=1 AND s.active=1 AND COALESCE(s.lifecycle_status,'active')='active'
             AND NOT EXISTS (SELECT 1 FROM market_price_programs p WHERE p.security_id=s.id AND p.status='active')
             AND NOT EXISTS (SELECT 1 FROM market_security_halts h WHERE h.security_id=s.id AND h.status='active')
             AND NOT EXISTS (SELECT 1 FROM market_security_delistings d WHERE d.security_id=s.id AND d.status='active')
           ORDER BY f.id""",
    )
    updated = 0
    for fund in funds:
        members = _rows(
            db,
            """SELECT m.weight,m.reference_price,s.price
               FROM market_index_members m JOIN market_securities s ON s.id=m.security_id
               WHERE m.fund_id=? AND s.active=1 AND COALESCE(s.lifecycle_status,'active')='active'
               ORDER BY m.rank,m.security_id""",
            (fund["id"],),
        )
        if not members:
            continue
        new_nav = index_nav(
            float(fund.get("base_nav") or 100),
            [(float(item.get("weight") or 0), float(item.get("price") or 0), float(item.get("reference_price") or 0)) for item in members],
            config.price_floor,
        )
        old_nav = max(config.price_floor, float(fund.get("price") or config.price_floor))
        movement = ((new_nav / old_nav) - 1) * 100 if old_nav else 0.0
        changed = abs(new_nav - old_nav) >= 0.0001
        if changed:
            db.execute(
                "UPDATE market_securities SET previous_price=price,price=?,updated_at=? WHERE id=?",
                (new_nav, timestamp, fund["security_id"]),
            )
            # Keep the established anonymous market tape populated without
            # touching resident cash, holdings, buying power, or pending orders.
            issued = (_one(db, "SELECT issued_shares FROM market_securities WHERE id=?", (fund["security_id"],)) or {}).get("issued_shares") or 1_000_000
            volume = max(10.0, min(float(issued) * 0.01, float(issued) * (0.001 + abs(movement) * 0.0005)))
            buy_ratio = 0.72 if movement > 0 else 0.28
            db.execute(
                """INSERT INTO market_system_trades
                   (security_id,buy_volume,sell_volume,buy_trade_count,sell_trade_count,reference_price,
                    price_change_percent,source,rationale,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (fund["security_id"], round(volume * buy_ratio, 6), round(volume * (1 - buy_ratio), 6),
                 2 if movement > 0 else 1, 1 if movement > 0 else 2, new_nav, round(movement, 4),
                 "fcx_engine_index_nav", f"{fund['ticker']} revalued from its configured FCX constituents.", timestamp),
            )
            db.execute(
                """INSERT INTO fcx_engine_audit_log
                   (cycle_id,security_id,ticker,action,shares,price,notional,reason_json,confidence,
                    market_sentiment,stock_sentiment,risk_score,created_at)
                   VALUES (?,?,?,'INDEX_REVALUE',0,?,0,?,100,50,50,0,?)""",
                (cycle_id, fund["security_id"], fund["ticker"], new_nav,
                 json.dumps(["Constituent-weighted FCX index NAV"], separators=(",", ":")), timestamp),
            )
            updated += 1
        last_history = _one(db, "SELECT price,recorded_at FROM market_price_history WHERE security_id=? ORDER BY id DESC LIMIT 1", (fund["security_id"],))
        last_time = _parse_time(last_history.get("recorded_at")) if last_history else None
        stale = not last_time or (utcnow() - last_time).total_seconds() >= 300
        if changed or not last_history or stale:
            db.execute(
                "INSERT INTO market_price_history (security_id,price,source,recorded_at) VALUES (?,?,'fcx_engine_index_nav',?)",
                (fund["security_id"], new_nav, timestamp),
            )
        db.execute("UPDATE market_index_funds SET last_valued_at=?,updated_at=? WHERE id=?", (timestamp, timestamp, fund["id"]))
    return updated


def _ensure_fundamentals(db: Any, securities: list[dict[str, Any]], seed: int) -> None:
    timestamp = now_iso()
    timestamp_dt = _parse_time(timestamp) or utcnow()
    rows: list[tuple[Any, ...]] = []
    for security in securities:
        if security.get("engine_fundamental_id") is not None:
            continue
        rng = random.Random(seed + int(security["id"]) * 7919)
        cap = max(100000.0, float(security.get("price") or 1) * float(security.get("issued_shares") or 1_000_000))
        revenue = cap * rng.uniform(0.08, 0.65)
        margin = rng.uniform(-0.18, 0.32)
        expenses = revenue * (1 - margin)
        profit = revenue - expenses
        assets = cap * rng.uniform(0.25, 1.8)
        debt_ratio = rng.uniform(0.05, 0.72)
        debt = assets * debt_ratio
        cash = assets * rng.uniform(0.04, 0.28)
        score = max(1.0, min(99.0, 52 + margin * 70 - debt_ratio * 25 + rng.uniform(-8, 8)))
        risk = max(1.0, min(99.0, 50 - score * 0.42 + debt_ratio * 35))
        bankruptcy = max(0.0, min(100.0, risk * 0.55 + (18 if profit < 0 else -8)))
        fair_value = max(0.01, float(security.get("price") or 1) * (0.65 + score / 100 * 0.7))
        rows.append((security["id"], revenue, expenses, profit, cash, assets, debt, debt, rng.uniform(-8, 18), rng.uniform(-12, 24), profit * 0.82, debt_ratio * 100, fair_value, score, risk, bankruptcy, 50 + rng.uniform(-8, 8), 1 if profit < 0 else 0, "distressed" if bankruptcy >= 70 else "healthy", timestamp, timestamp))
    if rows:
        db.executemany(
            """INSERT INTO fcx_engine_company_fundamentals
               (security_id,revenue,expenses,profit,cash,assets,liabilities,debt,revenue_growth,profit_growth,
                cash_flow,debt_ratio,fair_value,fundamental_score,risk_score,bankruptcy_risk,company_sentiment,
                consecutive_losses,status,analyzed_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (security_id) DO NOTHING""",
            rows,
        )


def _refresh_market_maker_quotes(
    db: Any,
    securities: list[dict[str, Any]],
    config: EngineConfig,
    timestamp: str,
    seed: int,
) -> int:
    provider_count = int((
        _one(
            db,
            "SELECT COUNT(*) AS count FROM fcx_engine_npc_investors WHERE status='active' AND personality='market_maker'",
        ) or {}
    ).get("count") or 0)
    refreshed = 0
    for security in securities:
        rng = random.Random(seed + int(security["id"]) * 15485863)
        quote = market_maker_quote(
            price=float(security.get("price") or config.price_floor),
            volatility=float(security.get("volatility") or 35),
            configured_spread_percent=config.market_maker_spread_percent,
            issued_shares=float(security.get("issued_shares") or 1_000_000),
            provider_count=provider_count,
            depth_factor=rng.uniform(0.75, 1.25) * config.market_maker_depth_multiplier,
            price_floor=config.price_floor,
        )
        db.execute(
            """INSERT INTO fcx_engine_liquidity_quotes
               (security_id,bid_price,ask_price,spread_percent,bid_depth,ask_depth,provider_count,updated_at)
               VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (security_id) DO UPDATE SET
               bid_price=excluded.bid_price,ask_price=excluded.ask_price,
               spread_percent=excluded.spread_percent,bid_depth=excluded.bid_depth,
               ask_depth=excluded.ask_depth,provider_count=excluded.provider_count,updated_at=excluded.updated_at""",
            (
                security["id"], quote.bid_price, quote.ask_price, quote.spread_percent,
                quote.bid_depth, quote.ask_depth, provider_count, timestamp,
            ),
        )
        refreshed += 1
    return refreshed


def _begin_cycle(db: Any, cycle_key: str, seed: int) -> tuple[int, str, float]:
    started = now_iso()
    token = hashlib.sha256(f"{cycle_key}:{started}:{seed}".encode()).hexdigest()[:32]
    row = db.execute(
        """INSERT INTO fcx_engine_cycle_log (cycle_key,status,cycle_token,started_at)
           VALUES (?,'running',?,?) RETURNING id""",
        (cycle_key, token, started),
    ).fetchone()
    return int(row["id"]), started, time.monotonic()


def _finish_cycle(db: Any, cycle_id: int, clock: float, result: dict[str, Any], error: str = "") -> None:
    db.execute(
        """UPDATE fcx_engine_cycle_log SET status=?,completed_at=?,duration_ms=?,investors_evaluated=?,
                  trades_executed=?,securities_moved=?,volume=?,summary_json=?,error_message=? WHERE id=?""",
        (
            "failed" if error else "complete", now_iso(), int((time.monotonic() - clock) * 1000),
            int(result.get("investors_evaluated") or 0), int(result.get("trades_executed") or 0),
            int(result.get("securities_moved") or 0), float(result.get("volume") or 0),
            json.dumps(result, separators=(",", ":"), default=str), str(error or "")[:1000], cycle_id,
        ),
    )


def _resume_expired_circuit_breakers(db: Any, timestamp: str) -> int:
    resumed = _rows(
        db,
        """UPDATE market_security_halts
           SET status='resumed',resumed_by_name='FCX Engine',resumed_at=?,
               resume_note='Automatic circuit-breaker window completed.'
           WHERE status='active' AND engine_managed=1
             AND automatic_resume_at IS NOT NULL AND automatic_resume_at<=?
           RETURNING id""",
        (timestamp, timestamp),
    )
    return len(resumed)


def _circuit_breaker_scan(db: Any, config: EngineConfig, timestamp: str) -> dict[str, Any]:
    if not config.halt_enabled:
        return {"triggered": 0, "tickers": []}
    current_time = _parse_time(timestamp) or utcnow()
    securities = _rows(
        db,
        """SELECT s.id,s.ticker,s.price FROM market_securities s
           WHERE s.active=1 AND COALESCE(s.lifecycle_status,'active')='active'
             AND COALESCE(s.security_type,'stock')<>'fund'
             AND NOT EXISTS (SELECT 1 FROM market_security_halts h
                             WHERE h.security_id=s.id AND h.status='active')""",
    )
    triggered: list[dict[str, Any]] = []
    windows = (
        ("30m", 30, config.circuit_breaker_30m_percent, config.circuit_breaker_30m_duration_minutes),
        ("10m", 10, config.circuit_breaker_10m_percent, config.circuit_breaker_10m_duration_minutes),
    )
    for security in securities:
        price = max(config.price_floor, float(security.get("price") or config.price_floor))
        candidates: list[tuple[str, float, float, int]] = []
        for window, minutes, threshold, duration in windows:
            cutoff = (current_time - dt.timedelta(minutes=minutes)).isoformat()
            reference = _one(
                db,
                """SELECT price FROM market_price_history WHERE security_id=? AND recorded_at>=?
                   ORDER BY recorded_at ASC,id ASC LIMIT 1""",
                (security["id"], cutoff),
            )
            if not reference:
                continue
            base = max(config.price_floor, float(reference.get("price") or price))
            movement = (price / base - 1.0) * 100.0
            if abs(movement) >= threshold:
                candidates.append((window, movement, threshold, duration))
        if not candidates:
            continue
        window, movement, threshold, duration = candidates[0]
        resume_at = (current_time + dt.timedelta(minutes=duration)).isoformat()
        case_reference = f"FCX-CB-{security['ticker']}-{current_time.strftime('%Y%m%d%H%M%S')}"
        inserted = db.execute(
            """INSERT INTO market_security_halts
               (security_id,status,reason_code,reason_label,public_notice,case_reference,
                halted_by_name,halted_at,automatic_resume_at,engine_managed)
               VALUES (?,'active','ENGINE_CIRCUIT_BREAKER','Automatic circuit breaker',?,?,
                       'FCX Engine',?,?,1)
               ON CONFLICT DO NOTHING RETURNING id""",
            (
                security["id"],
                f"Trading paused after a {movement:+.2f}% move over {window}; automatic review window active.",
                case_reference,
                timestamp,
                resume_at,
            ),
        ).fetchone()
        if inserted:
            triggered.append({
                "ticker": str(security["ticker"]), "window": window,
                "movement_percent": round(movement, 4), "threshold_percent": threshold,
                "resume_at": resume_at,
            })
    return {"triggered": len(triggered), "tickers": triggered}


def _minute_cycle(db: Any, config: EngineConfig, cycle_id: int, seed: int) -> dict[str, Any]:
    timestamp = now_iso()
    circuit_breakers_resumed = _resume_expired_circuit_breakers(db, timestamp)
    securities = _load_securities(db)
    _ensure_fundamentals(db, securities, config.random_seed)
    securities = _load_securities(db)
    if not securities:
        return {"investors_evaluated": 0, "trades_executed": 0, "securities_moved": 0, "volume": 0, "message": "No operating securities"}
    timestamp_dt = _parse_time(timestamp) or utcnow()
    cutoff = (utcnow() - dt.timedelta(minutes=5)).isoformat()
    human = {
        int(row["security_id"]): row
        for row in _rows(
            db,
            """SELECT security_id,
                      COALESCE(SUM(CASE WHEN side='buy' THEN quantity ELSE 0 END),0) AS buy_volume,
                      COALESCE(SUM(CASE WHEN side='sell' THEN quantity ELSE 0 END),0) AS sell_volume
               FROM market_orders WHERE created_at>=? GROUP BY security_id""",
            (cutoff,),
        )
    }
    max_evaluations = max(
        10,
        min(config.population, int(config.execution_budget_per_tick * max(0.25, config.activity_multiplier))),
    )
    investors = _rows(
        db,
        """SELECT * FROM fcx_engine_npc_investors
           WHERE status='active' AND (next_action_at IS NULL OR next_action_at<=?)
           ORDER BY next_action_at NULLS FIRST,id LIMIT ? FOR UPDATE SKIP LOCKED""",
        (now_iso(), max_evaluations),
    )
    position_rows = _rows(
        db,
        """SELECT * FROM fcx_engine_npc_positions WHERE investor_id IN
           (SELECT id FROM fcx_engine_npc_investors WHERE status='active' ORDER BY id LIMIT ?)""",
        (max(config.population, 1),),
    )
    positions = {(int(row["investor_id"]), int(row["security_id"])): row for row in position_rows}
    short_rows = _rows(
        db,
        """SELECT * FROM fcx_engine_npc_shorts WHERE investor_id IN
           (SELECT id FROM fcx_engine_npc_investors WHERE status='active' ORDER BY id LIMIT ?)""",
        (max(config.population, 1),),
    )
    shorts = {(int(row["investor_id"]), int(row["security_id"])): row for row in short_rows}
    state = _one(db, "SELECT * FROM fcx_engine_state WHERE id=1") or {}
    event_effects, market_effect = _event_effects(db, securities, timestamp)
    market_sentiment = max(0.0, min(100.0, float(state.get("market_sentiment") or 50) + market_effect["sentiment"] * config.sentiment_sensitivity))
    npc_flow: dict[int, dict[str, float]] = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "buy_count": 0.0, "sell_count": 0.0})
    parent_flow, parent_fills = _execute_parent_order_children(db, config, cycle_id, securities, timestamp)
    for security_id, values in parent_flow.items():
        for key_name, value in values.items():
            npc_flow[security_id][key_name] += value
    squeeze_flow, squeeze_fills = _apply_short_squeeze_covers(db, config, cycle_id, securities, timestamp)
    for security_id, values in squeeze_flow.items():
        for key_name, value in values.items():
            npc_flow[security_id][key_name] += value
    # Parent-order slices settle before discretionary NPC decisions. Refresh the
    # in-memory long ledger so a second action in this same minute cannot write
    # an older quantity back over a freshly executed child order.
    if parent_fills or squeeze_fills:
        position_rows = _rows(
            db,
            """SELECT * FROM fcx_engine_npc_positions WHERE investor_id IN
               (SELECT id FROM fcx_engine_npc_investors WHERE status='active' ORDER BY id LIMIT ?)""",
            (max(config.population, 1),),
        )
        positions = {(int(row["investor_id"]), int(row["security_id"])): row for row in position_rows}
        short_rows = _rows(
            db,
            """SELECT * FROM fcx_engine_npc_shorts WHERE investor_id IN
               (SELECT id FROM fcx_engine_npc_investors WHERE status='active' ORDER BY id LIMIT ?)""",
            (max(config.population, 1),),
        )
        shorts = {(int(row["investor_id"]), int(row["security_id"])): row for row in short_rows}
    executed: list[dict[str, Any]] = []
    panic_limit = int(math.ceil(max_evaluations * config.panic_participation_percent / 100.0))
    panic_evaluated = 0
    for investor in investors:
        investor = _one(db, "SELECT * FROM fcx_engine_npc_investors WHERE id=?", (investor["id"],)) or investor
        personality = str(investor.get("personality") or "retail")
        if personality in config.paused_personalities:
            continue
        if personality == "panic":
            if panic_evaluated >= panic_limit:
                next_at = (utcnow() + dt.timedelta(seconds=60)).isoformat()
                db.execute(
                    "UPDATE fcx_engine_npc_investors SET next_action_at=?,updated_at=? WHERE id=?",
                    (next_at, timestamp, investor["id"]),
                )
                continue
            panic_evaluated += 1
        rng = random.Random(seed + int(investor["id"]) * 104729)
        if rng.random() > float(investor.get("trade_frequency") or 0.25) * config.activity_multiplier:
            next_at = (utcnow() + dt.timedelta(seconds=rng.randint(45, 420))).isoformat()
            db.execute("UPDATE fcx_engine_npc_investors SET next_action_at=?,updated_at=? WHERE id=?", (next_at, timestamp, investor["id"]))
            continue
        preferred = set(_json(investor.get("preferred_sectors_json"), []))
        candidates = [item for item in securities if str(item.get("ticker") or "").lower() not in config.paused_tickers]
        preferred_candidates = [item for item in candidates if item.get("sector") in preferred]
        security = rng.choice(preferred_candidates or candidates)
        key = (int(investor["id"]), int(security["id"]))
        position = positions.get(key) or {}
        short_position = shorts.get(key) or {}
        current_price = max(config.price_floor, float(security.get("price") or config.price_floor))
        previous_price = max(config.price_floor, float(security.get("previous_price") or current_price))
        momentum = (current_price / previous_price - 1) * 100 if previous_price else 0
        fair_value = float(security.get("fair_value") or current_price)
        valuation_gap = (fair_value / current_price - 1) * 100 if current_price else 0
        uncertainty = ipo_uncertainty_multiplier(
            security.get("ipo_activated_at"), timestamp_dt,
            config.ipo_uncertainty_days, config.ipo_uncertainty_max_multiplier,
        ) if config.ipo_uncertainty_enabled else 1.0
        decision = decide(
            personality,
            {
                "momentum": momentum,
                "valuation_gap": valuation_gap,
                "fundamental_score": security.get("fundamental_score"),
                "sentiment": (market_sentiment + float(security.get("company_sentiment") or 50) + event_effects[int(security["id"])]["sentiment"] * config.sentiment_sensitivity) / 2,
                "volatility": (float(security.get("volatility") or 35) + event_effects[int(security["id"])]["volatility"]) * uncertainty,
                "bankruptcy_risk": security.get("bankruptcy_risk"),
                "held_quantity": position.get("quantity") or 0,
            },
            investor,
            rng,
        )
        cash = max(0.0, float(investor.get("cash_balance") or 0))
        quantity_held = max(0.0, float(position.get("quantity") or 0))
        short_quantity = max(0.0, float(short_position.get("quantity") or 0))
        reserve = float(investor.get("min_cash_reserve_percent") or 20) / 100
        available = max(0.0, cash - float(investor.get("initial_capital") or cash) * reserve)
        order_fraction = min(config.max_order_percent, float(investor.get("max_position_percent") or 2) / 100) * rng.uniform(0.18, 1.0)
        if personality == "market_maker":
            order_fraction *= max(0.25, min(2.0, 0.35 / max(config.market_maker_spread_percent, 0.01)))
        side = ""
        quantity = 0.0
        is_cover = short_quantity > 0 and decision.score >= 4
        if is_cover:
            side = "buy"
            quantity = short_quantity if decision.score >= 14 else short_quantity * 0.5
        elif decision.action in ("BUY", "ACCUMULATE") and available >= current_price:
            side = "buy"
            quantity = min(available * order_fraction / current_price, max(1.0, float(security.get("issued_shares") or 1) * 0.0025))
        elif decision.action in ("SELL", "REDUCE", "LIQUIDATE") and quantity_held > 0:
            side = "sell"
            ratio = 1.0 if decision.action == "LIQUIDATE" else (0.5 if decision.action == "SELL" else 0.25)
            quantity = quantity_held * ratio
        elif decision.action == "SHORT" and config.short_selling_enabled and available >= current_price * 0.5:
            side = "sell"
            quantity = min((available * order_fraction * 2) / current_price, max(1.0, float(security.get("issued_shares") or 1) * 0.001))
        if not side or quantity <= 0:
            next_at = (utcnow() + dt.timedelta(seconds=rng.randint(60, 480))).isoformat()
            db.execute("UPDATE fcx_engine_npc_investors SET next_action_at=?,updated_at=?,memory_json=? WHERE id=?", (next_at, timestamp, json.dumps({"last_action": "HOLD", "ticker": security["ticker"], "reason": decision.reasons}, separators=(",", ":")), investor["id"]))
            continue
        quantity = round(max(0.000001, quantity), 6)
        notional = round(quantity * current_price, 2)
        average_cost = float(position.get("average_cost") or current_price)
        realized = 0.0
        if is_cover:
            quantity = min(quantity, short_quantity)
            notional = round(quantity * current_price, 2)
            short_average = float(short_position.get("average_entry") or current_price)
            existing_collateral = float(short_position.get("collateral") or 0)
            collateral_release = existing_collateral * quantity / max(short_quantity, 0.000001)
            realized = round((short_average - current_price) * quantity, 2)
            remaining_short = max(0.0, short_quantity - quantity)
            db.execute(
                """UPDATE fcx_engine_npc_shorts SET quantity=?,collateral=?,realized_pnl=realized_pnl+?,updated_at=?
                   WHERE investor_id=? AND security_id=?""",
                (remaining_short, max(0.0, existing_collateral - collateral_release), realized, timestamp, investor["id"], security["id"]),
            )
            cash_after = max(0.0, cash + collateral_release + realized)
            npc_flow[int(security["id"])]["buy"] += quantity
            npc_flow[int(security["id"])]["buy_count"] += 1
        elif side == "buy":
            new_quantity = quantity_held + quantity
            new_average = ((quantity_held * average_cost) + notional) / max(new_quantity, 0.000001)
            db.execute(
                """INSERT INTO fcx_engine_npc_positions (investor_id,security_id,quantity,average_cost,opened_at,updated_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT (investor_id,security_id) DO UPDATE SET
                   quantity=excluded.quantity,average_cost=excluded.average_cost,updated_at=excluded.updated_at""",
                (investor["id"], security["id"], new_quantity, new_average, timestamp, timestamp),
            )
            cash_after = max(0.0, cash - notional)
            npc_flow[int(security["id"])]["buy"] += quantity
            npc_flow[int(security["id"])]["buy_count"] += 1
        else:
            if decision.action == "SHORT":
                collateral = round(notional * 0.5, 2)
                short_average = float(short_position.get("average_entry") or current_price)
                new_short_quantity = short_quantity + quantity
                new_short_average = ((short_quantity * short_average) + notional) / max(new_short_quantity, 0.000001)
                db.execute(
                    """INSERT INTO fcx_engine_npc_shorts
                       (investor_id,security_id,quantity,average_entry,collateral,opened_at,updated_at)
                       VALUES (?,?,?,?,?,?,?) ON CONFLICT (investor_id,security_id) DO UPDATE SET
                       quantity=excluded.quantity,average_entry=excluded.average_entry,
                       collateral=fcx_engine_npc_shorts.collateral+excluded.collateral,updated_at=excluded.updated_at""",
                    (investor["id"], security["id"], new_short_quantity, new_short_average, collateral, timestamp, timestamp),
                )
                cash_after = max(0.0, cash - collateral)
            else:
                quantity = min(quantity, quantity_held)
                notional = round(quantity * current_price, 2)
                realized = round((current_price - average_cost) * quantity, 2)
                new_quantity = max(0.0, quantity_held - quantity)
                db.execute("UPDATE fcx_engine_npc_positions SET quantity=?,realized_pnl=realized_pnl+?,updated_at=? WHERE investor_id=? AND security_id=?", (new_quantity, realized, timestamp, investor["id"], security["id"]))
                cash_after = cash + notional
            npc_flow[int(security["id"])]["sell"] += quantity
            npc_flow[int(security["id"])]["sell_count"] += 1
        next_at = (utcnow() + dt.timedelta(seconds=rng.randint(30, max(60, int(420 / max(config.activity_multiplier, 0.1)))))).isoformat()
        db.execute(
            """UPDATE fcx_engine_npc_investors SET cash_balance=?,realized_pnl=realized_pnl+?,
               recent_wins=recent_wins+?,recent_losses=recent_losses+?,memory_json=?,next_action_at=?,updated_at=? WHERE id=?""",
            (cash_after, realized, 1 if realized > 0 else 0, 1 if realized < 0 else 0, json.dumps({"last_action": decision.action, "ticker": security["ticker"], "score": decision.score, "reason": decision.reasons}, separators=(",", ":")), next_at, timestamp, investor["id"]),
        )
        executed.append({"investor": investor, "security": security, "decision": decision, "side": side, "quantity": quantity, "price": current_price, "notional": notional})
    moved = 0
    total_volume = 0.0
    for security in securities:
        security_id = int(security["id"])
        flow = npc_flow[security_id]
        human_flow = human.get(security_id) or {}
        human_buy = float(human_flow.get("buy_volume") or 0) * config.human_priority
        human_sell = float(human_flow.get("sell_volume") or 0) * config.human_priority
        uncertainty = ipo_uncertainty_multiplier(
            security.get("ipo_activated_at"), timestamp_dt,
            config.ipo_uncertainty_days, config.ipo_uncertainty_max_multiplier,
        ) if config.ipo_uncertainty_enabled else 1.0
        quote = discover_price(
            price=float(security.get("price") or config.price_floor), human_buy=human_buy, human_sell=human_sell,
            npc_buy=flow["buy"] * (1 - config.human_priority), npc_sell=flow["sell"] * (1 - config.human_priority),
            issued_shares=float(security.get("issued_shares") or 1_000_000), volatility=max(0.0, min(100.0, (float(security.get("volatility") or 35) + event_effects[security_id]["volatility"]) * uncertainty)),
            market_sentiment=market_sentiment, company_sentiment=max(0.0, min(100.0, float(security.get("company_sentiment") or 50) + event_effects[security_id]["sentiment"] * config.sentiment_sensitivity)),
            fundamental_score=float(security.get("fundamental_score") or 50), fair_value=float(security.get("fair_value") or security.get("price") or 1),
            cap_percent=config.minute_cap_percent, price_floor=config.price_floor,
        )
        volume = flow["buy"] + flow["sell"] + human_buy + human_sell
        total_volume += volume * quote.old_price
        if abs(quote.new_price - quote.old_price) >= 0.00005:
            db.execute("UPDATE market_securities SET previous_price=price,price=?,updated_at=? WHERE id=?", (quote.new_price, timestamp, security_id))
            db.execute("INSERT INTO market_price_history (security_id,price,source,recorded_at) VALUES (?,?,'fcx_engine',?)", (security_id, quote.new_price, timestamp))
            moved += 1
        if volume > 0:
            db.execute(
                """INSERT INTO market_system_trades
                   (security_id,buy_volume,sell_volume,buy_trade_count,sell_trade_count,reference_price,
                    price_change_percent,source,rationale,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (security_id, flow["buy"], flow["sell"], int(flow["buy_count"]), int(flow["sell_count"]), quote.new_price, quote.movement_percent, "fcx_engine", "; ".join(quote.explanation)[:300], timestamp),
            )
    for trade in executed:
        db.execute(
            """INSERT INTO fcx_engine_audit_log
               (cycle_id,investor_id,personality,security_id,ticker,action,shares,price,notional,reason_json,
                confidence,market_sentiment,stock_sentiment,risk_score,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cycle_id, trade["investor"]["id"], trade["investor"]["personality"], trade["security"]["id"], trade["security"]["ticker"], trade["decision"].action, trade["quantity"], trade["price"], trade["notional"], json.dumps(trade["decision"].reasons, separators=(",", ":")), trade["decision"].confidence, market_sentiment, trade["security"].get("company_sentiment") or 50, trade["security"].get("risk_score") or 0, timestamp),
        )
    index_funds_revalued = _revalue_index_funds(db, config, cycle_id, timestamp)
    price_programs = _apply_price_programs(db, config, cycle_id, timestamp)
    liquidity_quotes = _refresh_market_maker_quotes(db, _load_securities(db), config, timestamp, seed)
    return {"investors_evaluated": len(investors), "trades_executed": len(executed) + len(parent_fills) + len(squeeze_fills), "parent_order_fills": len(parent_fills), "short_squeeze_covers": len(squeeze_fills), "active_events": int(market_effect["events"]), "securities_moved": moved + int(price_programs["moved"]), "index_funds_revalued": index_funds_revalued, "price_programs": price_programs, "liquidity_quotes": liquidity_quotes, "volume": round(total_volume, 2), "human_priority_percent": round(config.human_priority * 100, 2), "execution_budget": max_evaluations, "panic_evaluated": panic_evaluated, "circuit_breakers_resumed": circuit_breakers_resumed}


def _five_minute_cycle(db: Any, config: EngineConfig, seed: int) -> dict[str, Any]:
    cutoff = (utcnow() - dt.timedelta(minutes=30)).isoformat()
    rows = _rows(db, "SELECT previous_price,price,volatility FROM market_securities WHERE active=1 AND COALESCE(lifecycle_status,'active')='active' AND COALESCE(security_type,'stock')<>'fund'")
    changes = [((float(row["price"]) / max(float(row.get("previous_price") or row["price"]), config.price_floor)) - 1) * 100 for row in rows]
    breadth = (sum(1 for value in changes if value >= 0) / max(1, len(changes))) * 100
    momentum = sum(changes) / max(1, len(changes))
    volatility = sum(float(row.get("volatility") or 35) for row in rows) / max(1, len(rows))
    flow = _one(db, "SELECT COALESCE(SUM(buy_volume),0) AS buys,COALESCE(SUM(sell_volume),0) AS sells FROM market_system_trades WHERE created_at>=?", (cutoff,)) or {}
    buys, sells = float(flow.get("buys") or 0), float(flow.get("sells") or 0)
    sentiment = max(0.0, min(100.0, 50 + momentum * 1.8 + ((buys - sells) / max(1.0, buys + sells)) * 18))
    speculative = (_one(db, "SELECT 100.0*COUNT(*)/NULLIF((SELECT COUNT(*) FROM fcx_engine_npc_investors),0) AS pct FROM fcx_engine_npc_investors WHERE personality IN ('speculator','day_trader','momentum')") or {}).get("pct") or 0
    gauge = fear_greed(momentum, volatility, breadth, sentiment, float(speculative))
    regime = regime_for(sentiment, volatility, momentum)
    db.execute("UPDATE fcx_engine_state SET market_sentiment=?,market_volatility=?,fear_greed=?,regime=?,updated_at=? WHERE id=1", (sentiment, volatility, gauge, regime, now_iso()))
    return {"securities": len(rows), "breadth": round(breadth, 2), "momentum": round(momentum, 4), "sentiment": round(sentiment, 2), "volatility": round(volatility, 2), "fear_greed": gauge, "regime": regime}


def _fifteen_minute_cycle(db: Any, config: EngineConfig, seed: int) -> dict[str, Any]:
    rows = _rows(db, """SELECT s.*,f.* FROM market_securities s JOIN fcx_engine_company_fundamentals f ON f.security_id=s.id WHERE s.active=1 AND COALESCE(s.security_type,'stock')<>'fund'""")
    timestamp = now_iso()
    flags = 0
    for row in rows:
        price = max(config.price_floor, float(row.get("price") or 1))
        fair = max(config.price_floor, float(row.get("fair_value") or price))
        score = max(1.0, min(99.0, float(row.get("fundamental_score") or 50) + ((fair / price - 1) * 2)))
        risk = max(0.0, min(100.0, float(row.get("risk_score") or 25) + (1.5 if float(row.get("profit") or 0) < 0 else -0.5)))
        bankruptcy = max(0.0, min(100.0, risk * 0.7 + int(row.get("consecutive_losses") or 0) * 4))
        db.execute("UPDATE fcx_engine_company_fundamentals SET fundamental_score=?,risk_score=?,bankruptcy_risk=?,analyzed_at=?,updated_at=? WHERE security_id=?", (score, risk, bankruptcy, timestamp, timestamp, row["id"]))
        if risk >= 75:
            existing = _one(db, "SELECT id FROM fcx_engine_risk_flags WHERE status='open' AND flag_type='company_distress' AND security_id=?", (row["id"],))
            evidence = json.dumps({"risk_score": risk, "bankruptcy_risk": bankruptcy, "ticker": row["ticker"]}, separators=(",", ":"))
            if existing:
                db.execute("UPDATE fcx_engine_risk_flags SET severity=?,evidence_json=?,last_seen_at=? WHERE id=?", ("critical" if risk >= 90 else "high", evidence, timestamp, existing["id"]))
            else:
                db.execute("INSERT INTO fcx_engine_risk_flags (flag_type,severity,security_id,status,evidence_json,first_seen_at,last_seen_at) VALUES ('company_distress',?,?,'open',?,?,?)", ("critical" if risk >= 90 else "high", row["id"], evidence, timestamp, timestamp))
            flags += 1
    surveillance_cutoff = (utcnow() - dt.timedelta(minutes=15)).isoformat()
    account_flow = _rows(
        db,
        """SELECT o.security_id,o.account_id,a.user_id,
                  COALESCE(SUM(quantity),0) AS total_quantity,
                  COALESCE(SUM(CASE WHEN side='buy' THEN quantity ELSE 0 END),0) AS buy_quantity,
                  COALESCE(SUM(CASE WHEN side='sell' THEN quantity ELSE 0 END),0) AS sell_quantity,
                  COUNT(*) AS executions
           FROM market_orders o JOIN market_accounts a ON a.id=o.account_id
           WHERE o.created_at>=?
           GROUP BY o.security_id,o.account_id,a.user_id""",
        (surveillance_cutoff,),
    )
    totals: dict[int, float] = defaultdict(float)
    for item in account_flow:
        totals[int(item["security_id"])] += float(item.get("total_quantity") or 0)
    surveillance_flags = 0
    for item in account_flow:
        security_id = int(item["security_id"])
        quantity = float(item.get("total_quantity") or 0)
        total = totals.get(security_id, 0.0)
        concentration = quantity / max(1.0, total) * 100.0
        buys = float(item.get("buy_quantity") or 0)
        sells = float(item.get("sell_quantity") or 0)
        round_trip = min(buys, sells) / max(1.0, max(buys, sells)) * 100.0
        rapid_round_trip = round_trip >= config.rapid_round_trip_percent and int(item.get("executions") or 0) >= 4
        abnormal = concentration >= config.flow_concentration_percent and quantity >= 100.0
        wash_pattern = round_trip >= config.wash_round_trip_percent and int(item.get("executions") or 0) >= 6
        if not abnormal and not rapid_round_trip:
            continue
        flag_type = "wash_trading_pattern" if wash_pattern else ("rapid_round_trip" if rapid_round_trip else "flow_concentration")
        evidence = json.dumps({
            "account_id": item.get("account_id"), "user_id": item.get("user_id"), "quantity": round(quantity, 6),
            "market_share_percent": round(concentration, 2), "round_trip_percent": round(round_trip, 2),
            "executions": int(item.get("executions") or 0), "window_minutes": 15,
            "automatic_action": "none",
        }, separators=(",", ":"))
        existing = _one(db, "SELECT id FROM fcx_engine_risk_flags WHERE status='open' AND flag_type=? AND security_id=? AND user_id=?", (flag_type, security_id, item.get("user_id")))
        if existing:
            db.execute("UPDATE fcx_engine_risk_flags SET severity=?,evidence_json=?,last_seen_at=? WHERE id=?", ("high" if wash_pattern else "review", evidence, timestamp, existing["id"]))
        else:
            db.execute("INSERT INTO fcx_engine_risk_flags (flag_type,severity,security_id,user_id,status,evidence_json,first_seen_at,last_seen_at) VALUES (?,?,?,?,'open',?,?,?)", (flag_type, "high" if wash_pattern else "review", security_id, item.get("user_id"), evidence, timestamp, timestamp))
        surveillance_flags += 1
    security_by_id = {int(row["id"]): row for row in rows}
    participant_counts: dict[int, int] = defaultdict(int)
    directional_totals: dict[int, dict[str, float]] = defaultdict(lambda: {"buy": 0.0, "sell": 0.0})
    for item in account_flow:
        security_id = int(item["security_id"])
        if float(item.get("total_quantity") or 0) > 0:
            participant_counts[security_id] += 1
        directional_totals[security_id]["buy"] += float(item.get("buy_quantity") or 0)
        directional_totals[security_id]["sell"] += float(item.get("sell_quantity") or 0)
    for security_id, flow in directional_totals.items():
        gross = flow["buy"] + flow["sell"]
        issued = max(1.0, float((security_by_id.get(security_id) or {}).get("issued_shares") or 1))
        imbalance = abs(flow["buy"] - flow["sell"]) / max(1.0, gross) * 100.0
        flags_to_create: list[tuple[str, str, dict[str, Any]]] = []
        if gross / issued * 100.0 >= config.abnormal_volume_float_percent:
            flags_to_create.append(("abnormal_volume", "review", {"volume": gross, "percent_of_float": gross / issued * 100.0}))
        if participant_counts[security_id] >= config.coordinated_flow_min_participants and imbalance >= config.coordinated_flow_imbalance_percent and gross >= 100.0:
            flags_to_create.append(("coordinated_flow_review", "high", {"participants": participant_counts[security_id], "directional_imbalance_percent": imbalance, "volume": gross}))
        for flag_type, severity, details in flags_to_create:
            evidence = json.dumps({**details, "window_minutes": 15, "automatic_action": "none"}, separators=(",", ":"))
            existing = _one(db, "SELECT id FROM fcx_engine_risk_flags WHERE status='open' AND flag_type=? AND security_id=? AND user_id IS NULL", (flag_type, security_id))
            if existing:
                db.execute("UPDATE fcx_engine_risk_flags SET severity=?,evidence_json=?,last_seen_at=? WHERE id=?", (severity, evidence, timestamp, existing["id"]))
            else:
                db.execute("INSERT INTO fcx_engine_risk_flags (flag_type,severity,security_id,status,evidence_json,first_seen_at,last_seen_at) VALUES (?,?,?,'open',?,?,?)", (flag_type, severity, security_id, evidence, timestamp, timestamp))
            surveillance_flags += 1
    return {"companies_analyzed": len(rows), "risk_flags": flags, "surveillance_flags": surveillance_flags}


def _thirty_minute_cycle(db: Any, config: EngineConfig, seed: int) -> dict[str, Any]:
    distressed = _rows(db, """SELECT s.id,s.ticker,s.price,f.risk_score,f.bankruptcy_risk,f.consecutive_losses
        FROM market_securities s JOIN fcx_engine_company_fundamentals f ON f.security_id=s.id
        WHERE s.active=1 AND COALESCE(s.lifecycle_status,'active')='active'
          AND COALESCE(s.security_type,'stock')<>'fund' AND (f.risk_score>=? OR f.bankruptcy_risk>=?)""",
        (config.bankruptcy_watch_threshold, config.bankruptcy_watch_threshold))
    timestamp = now_iso()
    halts = 0
    chapter_11 = 0
    chapter_7 = 0
    delisted = 0
    for row in distressed:
        risk = max(float(row.get("risk_score") or 0), float(row.get("bankruptcy_risk") or 0))
        losses = int(row.get("consecutive_losses") or 0)
        if config.halt_enabled and risk >= config.halt_risk_threshold:
            if not _one(db, "SELECT id FROM market_security_halts WHERE security_id=? AND status='active'", (row["id"],)):
                db.execute("""INSERT INTO market_security_halts
                    (security_id,status,reason_code,reason_label,public_notice,case_reference,halted_by_name,halted_at)
                    VALUES (?,'active','ENGINE_VOLATILITY','Automated volatility protection','FCX circuit breaker review in progress.','','FCX Engine',?)""", (row["id"], timestamp))
                halts += 1
        if config.bankruptcy_enabled and risk >= config.bankruptcy_ch7_threshold and losses >= config.bankruptcy_ch7_loss_cycles:
            db.execute("""UPDATE market_securities SET previous_price=price,price=0,active=0,lifecycle_status='bankrupt',
                bankruptcy_chapter='Chapter 7',bankruptcy_reason='FCX engine insolvency threshold',bankruptcy_at=?,updated_at=? WHERE id=?""", (timestamp, timestamp, row["id"]))
            db.execute("UPDATE fcx_engine_company_fundamentals SET status='chapter_7',updated_at=? WHERE security_id=?", (timestamp, row["id"]))
            db.execute("UPDATE fcx_engine_parent_orders SET status='cancelled',updated_at=? WHERE security_id=? AND status='active'", (timestamp, row["id"]))
            chapter_7 += 1
            continue
        if config.bankruptcy_enabled and risk >= config.bankruptcy_ch11_threshold:
            db.execute("UPDATE fcx_engine_company_fundamentals SET status='chapter_11',updated_at=? WHERE security_id=?", (timestamp, row["id"]))
            chapter_11 += 1
        else:
            db.execute("UPDATE fcx_engine_company_fundamentals SET status='watched',updated_at=? WHERE security_id=?", (timestamp, row["id"]))
        if config.delisting_enabled and float(row.get("price") or 0) <= config.delisting_price_floor and losses >= config.bankruptcy_ch7_loss_cycles:
            if not _one(db, "SELECT id FROM market_security_delistings WHERE security_id=? AND status='active'", (row["id"],)):
                db.execute("""INSERT INTO market_security_delistings
                    (security_id,status,reason_code,reason_label,public_notice,case_reference,delisted_by_name,delisted_at)
                    VALUES (?,'active','ENGINE_LISTING_STANDARD','Automated listing-standard review','Trading is unavailable pending a listing-standard review.','','FCX Engine',?)""", (row["id"], timestamp))
                db.execute("UPDATE market_securities SET lifecycle_status='delisted',updated_at=? WHERE id=?", (timestamp, row["id"]))
                db.execute("UPDATE fcx_engine_company_fundamentals SET status='delisted',updated_at=? WHERE security_id=?", (timestamp, row["id"]))
                db.execute("UPDATE fcx_engine_parent_orders SET status='cancelled',updated_at=? WHERE security_id=? AND status='active'", (timestamp, row["id"]))
                delisted += 1
    circuit_breakers = _circuit_breaker_scan(db, config, timestamp)
    return {"distressed_companies": len(distressed), "protective_halts": halts, "circuit_breakers": circuit_breakers, "chapter_11": chapter_11, "chapter_7": chapter_7, "delisted": delisted, "bankruptcy_automation": config.bankruptcy_enabled, "delisting_automation": config.delisting_enabled}


def _event_severity(sentiment: float, revenue: float, volatility: float) -> str:
    impact = max(abs(float(sentiment)), abs(float(revenue)), abs(float(volatility)))
    if impact >= 15:
        return "SYSTEMIC"
    if impact >= 11:
        return "CRITICAL"
    if impact >= 8:
        return "MAJOR"
    if impact >= 4:
        return "MODERATE"
    return "MINOR"


def _hourly_cycle(db: Any, config: EngineConfig, seed: int) -> dict[str, Any]:
    if not config.events_enabled:
        return {"event_created": False, "reason": "Events disabled"}
    rng = random.Random(seed)
    if rng.random() > config.event_probability_percent / 100.0:
        return {"event_created": False, "reason": "No event passed the seeded rarity gate"}
    sectors = [row["sector"] for row in _rows(db, "SELECT DISTINCT sector FROM market_securities WHERE active=1 AND COALESCE(security_type,'stock')<>'fund'")]
    affected = rng.sample(sectors, min(len(sectors), rng.randint(1, max(1, min(3, len(sectors)))))) if sectors else ["General"]
    kind, title, sentiment, revenue, volatility = rng.choice([
        ("economic_boom", "Regional demand accelerates", 6, 4, 2),
        ("recession", "Economic contraction warning", -9, -7, 8),
        ("war", "Geopolitical conflict disrupts trade corridors", -12, -9, 15),
        ("pandemic", "Public-health emergency changes demand", -11, -10, 14),
        ("commodity_spike", "Critical commodity prices surge", -3, -5, 10),
        ("technology_breakthrough", "Commercial breakthrough announced", 8, 7, 5),
        ("regulation_change", "FEC rulemaking affects listed issuers", -2, -1, 5),
        ("political_crisis", "Political uncertainty reaches the market", -8, -4, 11),
        ("supply_chain_disruption", "Supply network disruption reaches issuers", -6, -7, 11),
        ("currency_crisis", "Currency instability raises funding risk", -13, -8, 16),
        ("energy_shortage", "Energy availability constrains production", -7, -9, 10),
        ("labor_strike", "Major labor stoppage affects output", -5, -8, 6),
        ("financial_scandal", "Financial reporting allegations emerge", -14, -8, 13),
        ("infrastructure_failure", "Infrastructure failure interrupts commerce", -7, -8, 9),
        ("trade_war", "Tariff escalation weighs on cross-border demand", -8, -6, 10),
        ("cyberattack", "Coordinated cyberattack disrupts listed companies", -10, -7, 14),
        ("consumer_boom", "Household demand expands across sectors", 7, 6, 3),
        ("credit_crunch", "Credit conditions tighten sharply", -12, -9, 13),
        ("sector_bubble", "Speculative sector activity accelerates", 9, 3, 16),
    ])
    timestamp = now_iso()
    severity = _event_severity(sentiment, revenue, volatility)
    duration_floor = 12 if severity in ("CRITICAL", "SYSTEMIC") else 2
    duration_ceiling = 72 if severity in ("CRITICAL", "SYSTEMIC") else 24
    ends = (utcnow() + dt.timedelta(hours=rng.randint(duration_floor, duration_ceiling))).isoformat()
    db.execute("""INSERT INTO fcx_engine_economic_events
        (event_type,severity,title,affected_sectors_json,sentiment_impact,revenue_impact,volatility_impact,starts_at,ends_at,status,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,'active',?)""", (kind, severity, title, json.dumps(affected), sentiment, revenue, volatility, timestamp, ends, timestamp))
    db.execute("INSERT INTO fcx_engine_news (event_type,severity,reason,payload_json,created_at) VALUES (?,?,?,?,?)", (kind, severity, title, json.dumps({"sectors": affected, "sentiment_impact": sentiment, "revenue_impact": revenue, "volatility_impact": volatility}), timestamp))
    return {"event_created": True, "event_type": kind, "severity": severity, "title": title, "sectors": affected}


def _six_hour_cycle(db: Any, config: EngineConfig, seed: int) -> dict[str, Any]:
    investors = _rows(db, "SELECT * FROM fcx_engine_npc_investors WHERE status='active' AND personality IN ('institutional','whale') ORDER BY id")
    securities = _load_securities(db)
    created = 0
    buy_orders = 0
    sell_orders = 0
    rng = random.Random(seed)
    timestamp = now_iso()
    for investor in investors[:25]:
        if not securities or rng.random() > 0.45:
            continue
        holdings = _rows(
            db,
            """SELECT p.security_id,p.quantity,s.ticker,s.price FROM fcx_engine_npc_positions p
               JOIN market_securities s ON s.id=p.security_id
               WHERE p.investor_id=? AND p.quantity>0 AND s.active=1
                 AND COALESCE(s.lifecycle_status,'active')='active' AND COALESCE(s.security_type,'stock')<>'fund'""",
            (investor["id"],),
        )
        liquidation = bool(holdings) and rng.random() < 0.35
        if liquidation:
            held = rng.choice(holdings)
            security = next((item for item in securities if int(item["id"]) == int(held["security_id"])), None)
            if not security:
                continue
            side = "sell"
            total = max(0.000001, float(held.get("quantity") or 0) * rng.uniform(0.25, 0.75))
            rationale = "Institutional liquidation split to limit market impact."
        else:
            security = rng.choice(securities)
            side = "buy"
            total = max(1.0, float(investor.get("cash_balance") or 0) * min(config.max_order_percent, 0.08) / max(config.price_floor, float(security.get("price") or 1)))
            rationale = "Institutional accumulation split to limit market impact."
        if _one(db, "SELECT id FROM fcx_engine_parent_orders WHERE investor_id=? AND security_id=? AND status='active'", (investor["id"], security["id"])):
            continue
        child = max(1.0, total / rng.randint(6, 20))
        db.execute("""INSERT INTO fcx_engine_parent_orders
            (investor_id,security_id,side,total_quantity,remaining_quantity,child_size,status,rationale,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'active',?,?,?)""", (investor["id"], security["id"], side, total, total, child, rationale, timestamp, timestamp))
        created += 1
        buy_orders += 1 if side == "buy" else 0
        sell_orders += 1 if side == "sell" else 0
    return {"institutions_reviewed": len(investors), "parent_orders_created": created, "buy_parent_orders": buy_orders, "sell_parent_orders": sell_orders}


def _daily_cycle(db: Any, config: EngineConfig, seed: int) -> dict[str, Any]:
    rows = _rows(db, "SELECT * FROM fcx_engine_company_fundamentals ORDER BY security_id")
    timestamp = now_iso()
    rng = random.Random(seed)
    sectors: dict[str, list[float]] = defaultdict(list)
    security_rows_by_id = {int(row["id"]): row for row in _rows(db, "SELECT id,sector FROM market_securities WHERE COALESCE(security_type,'stock')<>'fund'")}
    event_effects, _ = _event_effects(db, list(security_rows_by_id.values()), timestamp)
    for row in rows:
        event_growth = event_effects.get(int(row["security_id"]), {}).get("revenue", 0.0)
        growth = rng.uniform(-4, 6) + (float(row.get("fundamental_score") or 50) - 50) * 0.04 + event_growth
        revenue = max(0.0, float(row.get("revenue") or 0) * (1 + growth / 100))
        expenses = max(0.0, float(row.get("expenses") or 0) * (1 + rng.uniform(-2, 4) / 100))
        profit = revenue - expenses
        losses = int(row.get("consecutive_losses") or 0) + 1 if profit < 0 else 0
        fair_value = max(config.price_floor, float(row.get("fair_value") or 1) * (1 + max(-8, min(8, growth)) / 100))
        db.execute("UPDATE fcx_engine_company_fundamentals SET revenue=?,expenses=?,profit=?,revenue_growth=?,profit_growth=?,cash_flow=?,fair_value=?,consecutive_losses=?,updated_at=? WHERE security_id=?", (revenue, expenses, profit, growth, growth, profit * 0.82, fair_value, losses, timestamp, row["security_id"]))
    security_rows = _rows(db, "SELECT sector,price,previous_price,volatility FROM market_securities WHERE active=1 AND COALESCE(security_type,'stock')<>'fund'")
    for security in security_rows:
        sectors[str(security.get("sector") or "General")].append((float(security["price"]) / max(config.price_floor, float(security.get("previous_price") or security["price"])) - 1) * 100)
    for sector, changes in sectors.items():
        performance = sum(changes) / max(1, len(changes))
        db.execute("""INSERT INTO fcx_engine_sector_state (sector,sentiment,performance,volatility,event_impact,updated_at)
            VALUES (?,?,?,?,0,?) ON CONFLICT (sector) DO UPDATE SET sentiment=excluded.sentiment,
            performance=excluded.performance,volatility=excluded.volatility,updated_at=excluded.updated_at""", (sector, max(0, min(100, 50 + performance * 2)), performance, min(100, abs(performance) * 4 + 25), timestamp))
    return {"companies_updated": len(rows), "sectors_updated": len(sectors)}


def _cycle_body(db: Any, cycle_key: str, config: EngineConfig, cycle_id: int, seed: int) -> dict[str, Any]:
    if cycle_key == "minute":
        return _minute_cycle(db, config, cycle_id, seed)
    if cycle_key == "five_minute":
        return _five_minute_cycle(db, config, seed)
    if cycle_key == "fifteen_minute":
        return _fifteen_minute_cycle(db, config, seed)
    if cycle_key == "thirty_minute":
        return _thirty_minute_cycle(db, config, seed)
    if cycle_key == "hourly":
        return _hourly_cycle(db, config, seed)
    if cycle_key == "six_hour":
        return _six_hour_cycle(db, config, seed)
    if cycle_key == "daily":
        return _daily_cycle(db, config, seed)
    raise ValueError("Unknown FCX engine cycle")


def run_manual_cycle(db: Any, settings: dict[str, Any], cycle_key: str = "minute") -> dict[str, Any]:
    cycle_key = str(cycle_key or "minute").strip().lower()
    if cycle_key not in CYCLE_ORDER:
        raise ValueError("Unsupported FCX engine cycle")
    config = EngineConfig.from_settings(settings)
    if config.kill_switch:
        return {"ok": False, "status": "killed", "cycle": cycle_key, "error": "FCX engine kill switch is active"}
    lock = _one(db, "SELECT pg_try_advisory_xact_lock(?) AS locked", (ENGINE_LOCK_ID,)) or {}
    if not bool(lock.get("locked")):
        return {"ok": False, "status": "busy", "cycle": cycle_key, "error": "Another FCX engine cycle owns the market lock"}
    count = int((_one(db, "SELECT COUNT(*) AS count FROM fcx_engine_npc_investors") or {}).get("count") or 0)
    if count < config.population:
        seed_investors(db, settings)
    seed = config.random_seed + int(utcnow().timestamp() // max(1, config.intervals[cycle_key]))
    cycle_id, started, clock = _begin_cycle(db, cycle_key, seed)
    result: dict[str, Any] = {}
    try:
        result = _cycle_body(db, cycle_key, config, cycle_id, seed)
        _finish_cycle(db, cycle_id, clock, result)
        state = _one(db, "SELECT last_cycle_state_json FROM fcx_engine_state WHERE id=1") or {}
        cycles = _json(state.get("last_cycle_state_json"), {})
        cycles[cycle_key] = now_iso()
        db.execute("""UPDATE fcx_engine_state SET status='online',last_cycle_state_json=?,last_heartbeat_at=?,
            last_error='',consecutive_failures=0,updated_at=? WHERE id=1""", (json.dumps(cycles, separators=(",", ":")), now_iso(), now_iso()))
        return {"ok": True, "status": "complete", "cycle": cycle_key, "cycle_id": cycle_id, "started_at": started, **result}
    except Exception as exc:
        _finish_cycle(db, cycle_id, clock, result, str(exc))
        db.execute("UPDATE fcx_engine_state SET status='degraded',last_error=?,consecutive_failures=consecutive_failures+1,last_heartbeat_at=?,updated_at=? WHERE id=1", (str(exc)[:1000], now_iso(), now_iso()))
        raise


def run_due_cycles(db: Any, settings: dict[str, Any], maximum_cycles: int = 2) -> dict[str, Any]:
    config = EngineConfig.from_settings(settings)
    if not config.enabled or config.kill_switch or config.activity_multiplier <= 0:
        status = "killed" if config.kill_switch else "paused"
        db.execute("UPDATE fcx_engine_state SET status=?,last_heartbeat_at=?,updated_at=? WHERE id=1", (status, now_iso(), now_iso()))
        return {"ok": True, "status": status, "cycles": []}
    state = _one(db, "SELECT * FROM fcx_engine_state WHERE id=1") or {}
    last_cycles = _json(state.get("last_cycle_state_json"), {})
    now = utcnow()
    due: list[str] = []
    for cycle_key in CYCLE_ORDER:
        previous = _parse_time(last_cycles.get(cycle_key))
        interval = config.intervals[cycle_key] / max(config.activity_multiplier, 0.1)
        if previous is None or (now - previous).total_seconds() >= interval:
            due.append(cycle_key)
    results = [run_manual_cycle(db, settings, cycle) for cycle in due[: max(1, min(7, int(maximum_cycles)))]]
    return {"ok": True, "status": "online", "cycles": results, "due": due}


def admin_snapshot(db: Any, settings: dict[str, Any]) -> dict[str, Any]:
    config = EngineConfig.from_settings(settings)
    state = _one(db, "SELECT * FROM fcx_engine_state WHERE id=1") or {}
    minute_cutoff = (utcnow() - dt.timedelta(minutes=1)).isoformat()
    personality = _rows(db, """SELECT personality,COUNT(*) AS investors,COALESCE(SUM(cash_balance),0) AS cash,
        COALESCE(SUM(realized_pnl),0) AS realized_pnl FROM fcx_engine_npc_investors GROUP BY personality ORDER BY personality""")
    counts = {
        "investors": int((_one(db, "SELECT COUNT(*) AS count FROM fcx_engine_npc_investors") or {}).get("count") or 0),
        "positions": int((_one(db, "SELECT COUNT(*) AS count FROM fcx_engine_npc_positions WHERE quantity>0") or {}).get("count") or 0),
        "short_positions": int((_one(db, "SELECT COUNT(*) AS count FROM fcx_engine_npc_shorts WHERE quantity>0") or {}).get("count") or 0),
        "parent_orders": int((_one(db, "SELECT COUNT(*) AS count FROM fcx_engine_parent_orders WHERE status='active'") or {}).get("count") or 0),
        "open_flags": int((_one(db, "SELECT COUNT(*) AS count FROM fcx_engine_risk_flags WHERE status='open'") or {}).get("count") or 0),
        "active_events": int((_one(db, "SELECT COUNT(*) AS count FROM fcx_engine_economic_events WHERE status='active' AND ends_at> ?", (now_iso(),)) or {}).get("count") or 0),
        "liquidity_quotes": int((_one(db, "SELECT COUNT(*) AS count FROM fcx_engine_liquidity_quotes") or {}).get("count") or 0),
        "active_halts": int((_one(db, "SELECT COUNT(*) AS count FROM market_security_halts WHERE status='active'") or {}).get("count") or 0),
        "operating_listings": int((_one(db, """SELECT COUNT(*) AS count FROM market_securities
            WHERE active=1 AND COALESCE(lifecycle_status,'active')='active'
              AND COALESCE(index_eligible,1)=1 AND security_type IN ('stock','volatile')""") or {}).get("count") or 0),
    }
    index_rows = _rows(db, """SELECT f.fund_key,s.ticker AS fund_ticker,COUNT(m.security_id) AS constituents
        FROM market_index_funds f
        JOIN market_securities s ON s.id=f.security_id
        LEFT JOIN market_index_members m ON m.fund_id=f.id
        GROUP BY f.id,f.fund_key,s.ticker ORDER BY f.fund_key""")
    index_counts = index_constituent_counts(index_rows)
    fund_units = _one(db, """SELECT COUNT(DISTINCT h.account_id) AS accounts,COALESCE(SUM(h.quantity),0) AS units
        FROM market_holdings h JOIN market_securities s ON s.id=h.security_id
        WHERE s.ticker IN ('FCXS','FCXV') AND h.quantity<>0""") or {}
    latest_deployment = _one(db, "SELECT * FROM fcx_engine_deployments ORDER BY id DESC LIMIT 1") or {}
    if latest_deployment:
        latest_deployment["details"] = _json(latest_deployment.pop("details_json", "{}"), {})
    readiness = {
        "ready": bool(
            counts["operating_listings"] >= 30
            and index_counts.get("FCXS", 0) >= 8
            and index_counts.get("FCXV", 0) >= 6
            and counts["investors"] >= config.population
            and config.enabled
            and not config.kill_switch
        ),
        "operating_listings": counts["operating_listings"],
        "target_listings": 30,
        "fcxs_constituents": index_counts.get("FCXS", 0),
        "fcxv_constituents": index_counts.get("FCXV", 0),
        "fund_accounts": int(fund_units.get("accounts") or 0),
        "fund_units": float(fund_units.get("units") or 0),
        "investors": counts["investors"],
        "target_investors": config.population,
        "engine_enabled": config.enabled,
        "kill_switch": config.kill_switch,
    }
    settings_payload = {
        "enabled": config.enabled, "kill_switch": config.kill_switch, "speed": config.speed,
        "random_seed": config.random_seed, "population": config.population, "total_capital": config.total_capital,
        "price_floor": config.price_floor, "minute_cap_percent": config.minute_cap_percent,
        "five_minute_cap_percent": config.five_minute_cap_percent, "thirty_minute_cap_percent": config.thirty_minute_cap_percent,
        "human_priority_percent": config.human_priority * 100, "max_order_percent": config.max_order_percent * 100,
        "market_maker_spread_percent": config.market_maker_spread_percent,
        "market_maker_depth_multiplier": config.market_maker_depth_multiplier,
        "execution_budget_per_tick": config.execution_budget_per_tick,
        "panic_participation_percent": config.panic_participation_percent,
        "events_enabled": config.events_enabled,
        "event_probability_percent": config.event_probability_percent, "sentiment_sensitivity": config.sentiment_sensitivity,
        "halt_risk_threshold": config.halt_risk_threshold,
        "circuit_breaker_10m_percent": config.circuit_breaker_10m_percent,
        "circuit_breaker_30m_percent": config.circuit_breaker_30m_percent,
        "circuit_breaker_10m_duration_minutes": config.circuit_breaker_10m_duration_minutes,
        "circuit_breaker_30m_duration_minutes": config.circuit_breaker_30m_duration_minutes,
        "abnormal_volume_float_percent": config.abnormal_volume_float_percent,
        "flow_concentration_percent": config.flow_concentration_percent,
        "rapid_round_trip_percent": config.rapid_round_trip_percent,
        "wash_round_trip_percent": config.wash_round_trip_percent,
        "coordinated_flow_imbalance_percent": config.coordinated_flow_imbalance_percent,
        "coordinated_flow_min_participants": config.coordinated_flow_min_participants,
        "bankruptcy_watch_threshold": config.bankruptcy_watch_threshold,
        "bankruptcy_ch11_threshold": config.bankruptcy_ch11_threshold, "bankruptcy_ch7_threshold": config.bankruptcy_ch7_threshold,
        "bankruptcy_ch7_loss_cycles": config.bankruptcy_ch7_loss_cycles, "delisting_price_floor": config.delisting_price_floor,
        "bankruptcy_enabled": config.bankruptcy_enabled, "delisting_enabled": config.delisting_enabled,
        "short_selling_enabled": config.short_selling_enabled, "halts_enabled": config.halt_enabled,
        "ipo_uncertainty_enabled": config.ipo_uncertainty_enabled,
        "ipo_uncertainty_days": config.ipo_uncertainty_days,
        "ipo_uncertainty_max_multiplier": config.ipo_uncertainty_max_multiplier,
        "paused_personalities": list(config.paused_personalities), "paused_tickers": list(config.paused_tickers),
        "distribution": config.distribution, "intervals": config.intervals,
    }
    state["last_cycle_state"] = _json(state.pop("last_cycle_state_json", "{}"), {})
    return {
        "settings": settings_payload,
        "state": state,
        "counts": counts,
        "deployment": {"latest": latest_deployment, "readiness": readiness},
        "capital": _capital_stats(db),
        "personalities": personality,
        "cycles": _rows(db, "SELECT * FROM fcx_engine_cycle_log ORDER BY id DESC LIMIT 24"),
        "audit": _rows(db, "SELECT * FROM fcx_engine_audit_log ORDER BY id DESC LIMIT 40"),
        "risk_flags": _rows(db, """SELECT f.*,s.ticker,s.name FROM fcx_engine_risk_flags f
            LEFT JOIN market_securities s ON s.id=f.security_id WHERE f.status='open' ORDER BY f.last_seen_at DESC LIMIT 30"""),
        "events": _rows(db, "SELECT * FROM fcx_engine_economic_events ORDER BY id DESC LIMIT 20"),
        "corporate_actions": _rows(db, """SELECT a.*,s.ticker,s.name FROM fcx_engine_corporate_actions a
            JOIN market_securities s ON s.id=a.security_id ORDER BY a.id DESC LIMIT 30"""),
        "liquidity": _rows(db, """SELECT q.*,s.ticker,s.name,s.price
            FROM fcx_engine_liquidity_quotes q JOIN market_securities s ON s.id=q.security_id
            WHERE s.active=1 ORDER BY q.spread_percent DESC,s.ticker LIMIT 30"""),
        "sectors": _rows(db, "SELECT * FROM fcx_engine_sector_state ORDER BY sector"),
        "halts": _rows(db, """SELECT h.*,s.ticker,s.name FROM market_security_halts h
            JOIN market_securities s ON s.id=h.security_id WHERE h.status='active'
            ORDER BY h.halted_at DESC LIMIT 30"""),
        "watchlist": _rows(db, """SELECT s.ticker,s.name,s.price,s.lifecycle_status,
            f.status,f.risk_score,f.bankruptcy_risk,f.consecutive_losses
            FROM market_securities s JOIN fcx_engine_company_fundamentals f ON f.security_id=s.id
            WHERE s.active=1 AND (f.status<>'healthy' OR f.risk_score>=60 OR f.bankruptcy_risk>=60)
            ORDER BY GREATEST(f.risk_score,f.bankruptcy_risk) DESC,s.ticker LIMIT 30"""),
        "market_operations": _one(db, """SELECT
            (SELECT COUNT(*) FROM market_orders WHERE created_at>=?) AS resident_trades_last_minute,
            (SELECT COALESCE(SUM(gross_amount),0) FROM market_orders WHERE created_at>=?) AS resident_volume_last_minute,
            (SELECT COALESCE(MAX(gross_amount),0) FROM market_orders WHERE created_at>=?) AS largest_resident_trade_last_minute,
            (SELECT COUNT(*) FROM fcx_engine_audit_log WHERE created_at>=?) AS engine_executions_last_minute,
            (SELECT COALESCE(SUM(notional),0) FROM fcx_engine_audit_log WHERE created_at>=?) AS engine_volume_last_minute,
            (SELECT COALESCE(SUM(price*issued_shares),0) FROM market_securities WHERE active=1) AS total_market_cap""",
            (minute_cutoff, minute_cutoff, minute_cutoff, minute_cutoff, minute_cutoff)) or {},
        "investor_leaders": _rows(db, """SELECT i.id,i.name,i.personality,i.cash_balance,i.realized_pnl,
            i.cash_balance + COALESCE((SELECT SUM(p.quantity*s.price) FROM fcx_engine_npc_positions p
                JOIN market_securities s ON s.id=p.security_id WHERE p.investor_id=i.id),0)
                + COALESCE((SELECT SUM(sh.collateral) FROM fcx_engine_npc_shorts sh WHERE sh.investor_id=i.id),0) AS gross_equity
            FROM fcx_engine_npc_investors i WHERE i.status='active'
            ORDER BY gross_equity DESC LIMIT 12"""),
        "securities": _rows(db, """SELECT s.id,s.ticker,s.name,s.security_type,s.sector,s.price,s.previous_price,s.volatility,
            f.fair_value,f.fundamental_score,f.risk_score,f.bankruptcy_risk,f.company_sentiment,f.status
            FROM market_securities s LEFT JOIN fcx_engine_company_fundamentals f ON f.security_id=s.id
            WHERE s.active=1 ORDER BY COALESCE(f.risk_score,0) DESC,s.ticker LIMIT 100"""),
    }


__all__ = ["admin_snapshot", "apply_dividend", "apply_stock_split", "ensure_schema", "run_due_cycles", "run_manual_cycle", "run_sandbox", "seed_investors"]
