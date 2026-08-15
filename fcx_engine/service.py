"""Optional standalone FastAPI host for the FCX autonomous engine.

The existing PWA can operate the same engine through ``app.py``.  This module
is an additive service boundary for deployments that want the market clock in
its own process.  It reads the shared ``system_settings`` table and only writes
engine-owned state plus the established Ravenhood quote/tape tables.
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Result

from .config import CYCLE_DEFAULTS, EngineConfig
from .engine import (
    admin_snapshot,
    apply_dividend,
    apply_stock_split,
    ensure_schema,
    run_due_cycles,
    run_manual_cycle,
    seed_investors,
)
from .sandbox import run_sandbox
from fcx_control.base_schema import ensure_base_schema
from fcx_control.schema import ensure_schema as ensure_control_schema


def _database_url() -> str:
    value = str(os.environ.get("FCX_DATABASE_URL") or "").strip()
    if not value:
        raise RuntimeError("FCX_DATABASE_URL is required for the standalone FCX service")
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    if value.startswith("postgresql://") and "+psycopg" not in value:
        value = "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


def _bind_qmarks(statement: str, params: tuple[Any, ...] | list[Any]) -> tuple[str, dict[str, Any]]:
    pieces = statement.split("?")
    if len(pieces) - 1 != len(params):
        raise ValueError("FCX SQL parameter count does not match the statement")
    bound: dict[str, Any] = {}
    sql = pieces[0]
    for index, value in enumerate(params):
        key = f"p{index}"
        sql += f":{key}" + pieces[index + 1]
        bound[key] = value
    return sql, bound


class ResultAdapter:
    def __init__(self, result: Result[Any]):
        self.result = result

    def fetchone(self) -> dict[str, Any] | None:
        row = self.result.mappings().fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.result.mappings().fetchall()]


class ConnectionAdapter:
    def __init__(self, connection: Connection):
        self.connection = connection

    def execute(self, statement: str, params: tuple[Any, ...] | list[Any] = ()) -> ResultAdapter:
        sql, bound = _bind_qmarks(statement, tuple(params))
        return ResultAdapter(self.connection.execute(text(sql), bound))

    def executemany(self, statement: str, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        sql, _ = _bind_qmarks(statement, tuple(rows[0]))
        values = []
        for row in rows:
            _, bound = _bind_qmarks(statement, tuple(row))
            values.append(bound)
        self.connection.execute(text(sql), values)


engine = create_engine(
    _database_url(),
    pool_size=max(1, int(os.environ.get("FCX_DB_POOL_SIZE", "2"))),
    max_overflow=max(0, int(os.environ.get("FCX_DB_MAX_OVERFLOW", "1"))),
    pool_pre_ping=True,
    pool_recycle=240,
)
scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
worker_guard = threading.Lock()
app = FastAPI(title="FCX Autonomous Market Engine", version="1.0.0")


@contextmanager
def transaction() -> Iterator[ConnectionAdapter]:
    with engine.begin() as connection:
        yield ConnectionAdapter(connection)


def load_settings(db: ConnectionAdapter) -> dict[str, Any]:
    rows = db.execute("SELECT setting_key,setting_value FROM system_settings").fetchall()
    return {str(row["setting_key"]): row.get("setting_value") for row in rows}


def save_setting(db: ConnectionAdapter, key: str, value: Any) -> None:
    if isinstance(value, bool):
        stored = "1" if value else "0"
    elif isinstance(value, (dict, list, tuple)):
        stored = json.dumps(value, separators=(",", ":"), sort_keys=isinstance(value, dict))
    else:
        stored = str(value)
    db.execute("""INSERT INTO system_settings (setting_key,setting_value,updated_at) VALUES (?,?,?)
        ON CONFLICT (setting_key) DO UPDATE SET
        setting_value=excluded.setting_value,updated_at=excluded.updated_at""",
        (key, stored, datetime.now(timezone.utc).isoformat()))


def require_admin(x_fcx_engine_key: str = Header(default="")) -> None:
    configured = str(os.environ.get("FCX_ENGINE_ADMIN_KEY") or "").strip()
    if not configured or x_fcx_engine_key != configured:
        raise HTTPException(status_code=403, detail="FCX engine administrator key required")


def scheduler_tick() -> None:
    if not worker_guard.acquire(blocking=False):
        return
    try:
        with transaction() as db:
            ensure_base_schema(db)
            ensure_schema(db)
            run_due_cycles(db, load_settings(db), maximum_cycles=2)
    finally:
        worker_guard.release()


@app.on_event("startup")
def startup() -> None:
    # Control identities and communities must exist before the exchange-owned
    # compatibility schema, whose foreign keys deliberately never reference a
    # CAD database.  Engine tables are created last.
    ensure_control_schema()
    with transaction() as db:
        ensure_base_schema(db)
        ensure_schema(db)
    run_scheduler = str(os.environ.get("FCX_RUN_SCHEDULER", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if run_scheduler and not scheduler.running:
        scheduler.add_job(scheduler_tick, "interval", seconds=15, id="fcx_market_clock", max_instances=1, coalesce=True)
        scheduler.start()


@app.on_event("shutdown")
def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
    engine.dispose()


@app.get("/api/health")
def health() -> dict[str, Any]:
    with transaction() as db:
        state = db.execute("SELECT status,last_heartbeat_at,last_error FROM fcx_engine_state WHERE id=1").fetchone() or {}
        db.execute("SELECT 1 AS ok").fetchone()
    return {"ok": True, "service": "fcx-engine", "state": state}


@app.get("/api/market/state")
def market_state() -> dict[str, Any]:
    with transaction() as db:
        settings = load_settings(db)
        snapshot = admin_snapshot(db, settings)
    return {"ok": True, "state": snapshot["state"], "counts": snapshot["counts"], "capital": snapshot["capital"]}


@app.get("/api/market/sentiment")
def market_sentiment() -> dict[str, Any]:
    with transaction() as db:
        state = db.execute("SELECT market_sentiment,fear_greed,regime,updated_at FROM fcx_engine_state WHERE id=1").fetchone() or {}
        sectors = db.execute("SELECT sector,sentiment,performance,event_impact,updated_at FROM fcx_engine_sector_state ORDER BY sector").fetchall()
    return {"ok": True, "sentiment": state, "sectors": sectors}


@app.get("/api/market/volatility")
def market_volatility() -> dict[str, Any]:
    with transaction() as db:
        state = db.execute("SELECT market_volatility,regime,updated_at FROM fcx_engine_state WHERE id=1").fetchone() or {}
        listings = db.execute("""SELECT ticker,name,sector,price,volatility,updated_at FROM market_securities
            WHERE active=1 AND COALESCE(lifecycle_status,'active')='active'
            ORDER BY volatility DESC,ticker LIMIT 100""").fetchall()
    return {"ok": True, "volatility": state, "listings": listings}


@app.get("/api/market/index")
def market_index() -> dict[str, Any]:
    with transaction() as db:
        funds = db.execute("""SELECT f.fund_key,f.display_name,f.risk_profile,f.last_valued_at,
            s.ticker,s.price,s.previous_price,s.updated_at,
            (SELECT COUNT(*) FROM market_index_members m WHERE m.fund_id=f.id) AS constituents
            FROM market_index_funds f JOIN market_securities s ON s.id=f.security_id
            WHERE f.enabled=1 ORDER BY f.fund_key""").fetchall()
        breadth = db.execute("""SELECT COUNT(*) AS listings,
            SUM(CASE WHEN price>previous_price THEN 1 ELSE 0 END) AS advancing,
            SUM(CASE WHEN price<previous_price THEN 1 ELSE 0 END) AS declining,
            SUM(CASE WHEN price=previous_price THEN 1 ELSE 0 END) AS unchanged,
            COALESCE(SUM(price*issued_shares),0) AS market_cap
            FROM market_securities WHERE active=1 AND COALESCE(lifecycle_status,'active')='active'
              AND COALESCE(security_type,'stock')<>'fund'""").fetchone() or {}
    return {"ok": True, "funds": funds, "breadth": breadth}


@app.get("/api/market/prices")
def prices(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    with transaction() as db:
        rows = db.execute("""SELECT id,ticker,name,sector,price,previous_price,volatility,updated_at
            FROM market_securities WHERE active=1 ORDER BY ticker LIMIT ?""", (limit,)).fetchall()
    return {"ok": True, "prices": rows}


@app.get("/api/market/fundamentals")
def fundamentals(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    with transaction() as db:
        rows = db.execute("""SELECT s.ticker,s.name,s.sector,f.* FROM fcx_engine_company_fundamentals f
            JOIN market_securities s ON s.id=f.security_id ORDER BY f.risk_score DESC,s.ticker LIMIT ?""", (limit,)).fetchall()
    return {"ok": True, "fundamentals": rows}


@app.get("/api/market/liquidity")
def market_liquidity(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    with transaction() as db:
        rows = db.execute("""SELECT s.ticker,s.name,s.sector,s.price,
            q.bid_price,q.ask_price,q.spread_percent,q.bid_depth,q.ask_depth,
            q.provider_count,q.updated_at
            FROM fcx_engine_liquidity_quotes q
            JOIN market_securities s ON s.id=q.security_id
            WHERE s.active=1
            ORDER BY q.spread_percent,s.ticker LIMIT ?""", (limit,)).fetchall()
    return {"ok": True, "liquidity": rows}


@app.get("/api/market/npcs")
def npcs(personality: str = "", limit: int = Query(default=100, ge=1, le=500), _: None = Depends(require_admin)) -> dict[str, Any]:
    with transaction() as db:
        if personality:
            rows = db.execute("SELECT * FROM fcx_engine_npc_investors WHERE personality=? ORDER BY id LIMIT ?", (personality, limit)).fetchall()
        else:
            rows = db.execute("SELECT * FROM fcx_engine_npc_investors ORDER BY id LIMIT ?", (limit,)).fetchall()
    return {"ok": True, "investors": rows}


@app.get("/api/market/npcs/{investor_id}/portfolio")
def npc_portfolio(investor_id: int, _: None = Depends(require_admin)) -> dict[str, Any]:
    with transaction() as db:
        investor = db.execute("SELECT * FROM fcx_engine_npc_investors WHERE id=?", (investor_id,)).fetchone()
        if not investor:
            raise HTTPException(status_code=404, detail="Investor not found")
        positions = db.execute("""SELECT p.*,s.ticker,s.name,s.price,p.quantity*s.price AS market_value
            FROM fcx_engine_npc_positions p JOIN market_securities s ON s.id=p.security_id
            WHERE p.investor_id=? AND p.quantity<>0 ORDER BY market_value DESC""", (investor_id,)).fetchall()
    return {"ok": True, "investor": investor, "positions": positions}


@app.get("/api/market/events")
def events(limit: int = Query(default=50, ge=1, le=250)) -> dict[str, Any]:
    with transaction() as db:
        rows = db.execute("SELECT * FROM fcx_engine_economic_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"ok": True, "events": rows}


@app.get("/api/market/news")
def news(limit: int = Query(default=50, ge=1, le=250)) -> dict[str, Any]:
    with transaction() as db:
        rows = db.execute("SELECT * FROM fcx_engine_news ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"ok": True, "news": rows}


@app.get("/api/stocks/{ticker}/analysis")
def stock_analysis(ticker: str) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    with transaction() as db:
        row = db.execute("""SELECT s.*,f.revenue,f.expenses,f.profit,f.cash,f.assets,f.liabilities,f.debt,
            f.revenue_growth,f.profit_growth,f.cash_flow,f.debt_ratio,f.fair_value,f.fundamental_score,
            f.risk_score,f.bankruptcy_risk,f.company_sentiment,f.consecutive_losses,f.status AS fundamental_status
            FROM market_securities s LEFT JOIN fcx_engine_company_fundamentals f ON f.security_id=s.id
            WHERE UPPER(s.ticker)=? LIMIT 1""", (ticker,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Ticker not found")
        flags = db.execute("SELECT * FROM fcx_engine_risk_flags WHERE security_id=? AND status='open' ORDER BY last_seen_at DESC", (row["id"],)).fetchall()
        events = db.execute("SELECT * FROM fcx_engine_news WHERE ticker=? ORDER BY id DESC LIMIT 25", (ticker,)).fetchall()
    return {"ok": True, "analysis": row, "risk_flags": flags, "news": events}


@app.get("/api/stocks/{ticker}/npc-activity")
def stock_npc_activity(ticker: str, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    with transaction() as db:
        security = db.execute("SELECT id,ticker,name,price FROM market_securities WHERE UPPER(ticker)=? LIMIT 1", (ticker,)).fetchone()
        if not security:
            raise HTTPException(status_code=404, detail="Ticker not found")
        audit = db.execute("""SELECT id,personality,action,shares,price,notional,reason_json,confidence,
            market_sentiment,stock_sentiment,risk_score,created_at
            FROM fcx_engine_audit_log WHERE security_id=? ORDER BY id DESC LIMIT ?""", (security["id"], limit)).fetchall()
        positions = db.execute("""SELECT i.name,i.personality,p.quantity,p.average_cost,p.realized_pnl,
            p.quantity*? AS market_value,p.updated_at FROM fcx_engine_npc_positions p
            JOIN fcx_engine_npc_investors i ON i.id=p.investor_id
            WHERE p.security_id=? AND p.quantity>0 ORDER BY market_value DESC LIMIT 100""", (float(security.get("price") or 0), security["id"])).fetchall()
    return {"ok": True, "security": security, "activity": audit, "positions": positions}


@app.get("/api/market/halts")
def market_halts() -> dict[str, Any]:
    with transaction() as db:
        rows = db.execute("""SELECT h.*,s.ticker,s.name FROM market_security_halts h
            JOIN market_securities s ON s.id=h.security_id ORDER BY h.halted_at DESC,h.id DESC LIMIT 250""").fetchall()
    return {"ok": True, "halts": rows}


@app.get("/api/market/bankruptcy/watchlist")
def bankruptcy_watchlist() -> dict[str, Any]:
    with transaction() as db:
        settings = load_settings(db)
        threshold = EngineConfig.from_settings(settings).bankruptcy_watch_threshold
        rows = db.execute("""SELECT s.ticker,s.name,s.sector,s.price,s.lifecycle_status,
            f.risk_score,f.bankruptcy_risk,f.consecutive_losses,f.profit,f.cash,f.debt,f.assets,f.updated_at
            FROM fcx_engine_company_fundamentals f JOIN market_securities s ON s.id=f.security_id
            WHERE f.risk_score>=? OR f.bankruptcy_risk>=?
            ORDER BY GREATEST(f.risk_score,f.bankruptcy_risk) DESC,s.ticker""", (threshold, threshold)).fetchall()
    return {"ok": True, "threshold": threshold, "watchlist": rows}


@app.get("/api/market/delisting/watchlist")
def delisting_watchlist() -> dict[str, Any]:
    with transaction() as db:
        settings = load_settings(db)
        config = EngineConfig.from_settings(settings)
        rows = db.execute("""SELECT s.ticker,s.name,s.sector,s.price,s.lifecycle_status,s.updated_at,
            f.risk_score,f.bankruptcy_risk,
            CASE WHEN s.price<=? THEN 'PRICE_TOO_LOW' ELSE 'RISK_REVIEW' END AS watch_reason
            FROM market_securities s LEFT JOIN fcx_engine_company_fundamentals f ON f.security_id=s.id
            WHERE s.active=1 AND COALESCE(s.security_type,'stock')<>'fund'
              AND (s.price<=? OR COALESCE(f.risk_score,0)>=?)
            ORDER BY s.price,s.ticker""", (config.delisting_price_floor, config.delisting_price_floor, config.bankruptcy_watch_threshold)).fetchall()
    return {"ok": True, "price_floor": config.delisting_price_floor, "watchlist": rows}


class CycleRequest(BaseModel):
    cycle: str = "minute"


class SeedRequest(BaseModel):
    replace: bool = False
    confirmation: str = ""


class SandboxRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=365)
    seed: int | None = None


class KillSwitchRequest(BaseModel):
    active: bool


class StockSplitRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)
    numerator: float = Field(ge=.01, le=1000)
    denominator: float = Field(ge=.01, le=1000)
    rationale: str = Field(min_length=5, max_length=1000)
    confirmation: str


class DividendRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)
    amount_per_share: float = Field(ge=.0001, le=1_000_000)
    rationale: str = Field(min_length=5, max_length=1000)
    confirmation: str


def _set_ticker_pause(ticker: str, paused: bool) -> dict[str, Any]:
    normalized = ticker.strip().lower()
    with transaction() as db:
        exists = db.execute("SELECT id FROM market_securities WHERE LOWER(ticker)=? LIMIT 1", (normalized,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Ticker not found")
        current = list(EngineConfig.from_settings(load_settings(db)).paused_tickers)
        values = set(current)
        if paused:
            values.add(normalized)
        else:
            values.discard(normalized)
        save_setting(db, "fcx_engine_paused_tickers", sorted(values))
        snapshot = admin_snapshot(db, load_settings(db))
    return {"ok": True, "ticker": normalized.upper(), "paused": paused, "engine": snapshot}


class EngineSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    kill_switch: bool | None = None
    speed: str | None = None
    random_seed: int | None = Field(default=None, ge=1, le=2147483647)
    population: int | None = Field(default=None, ge=1, le=5000)
    total_capital: float | None = Field(default=None, ge=1000, le=1_000_000_000_000)
    price_floor: float | None = Field(default=None, ge=.0001, le=1_000_000)
    minute_cap_percent: float | None = Field(default=None, ge=.01, le=100)
    five_minute_cap_percent: float | None = Field(default=None, ge=.01, le=200)
    thirty_minute_cap_percent: float | None = Field(default=None, ge=.01, le=500)
    human_priority_percent: float | None = Field(default=None, ge=0, le=100)
    max_order_percent: float | None = Field(default=None, ge=.01, le=50)
    market_maker_spread_percent: float | None = Field(default=None, ge=.01, le=25)
    market_maker_depth_multiplier: float | None = Field(default=None, ge=.1, le=10)
    execution_budget_per_tick: int | None = Field(default=None, ge=10, le=5000)
    panic_participation_percent: float | None = Field(default=None, ge=0, le=100)
    event_probability_percent: float | None = Field(default=None, ge=0, le=100)
    sentiment_sensitivity: float | None = Field(default=None, ge=0, le=5)
    halt_risk_threshold: float | None = Field(default=None, ge=50, le=100)
    circuit_breaker_10m_percent: float | None = Field(default=None, ge=.1, le=500)
    circuit_breaker_30m_percent: float | None = Field(default=None, ge=.1, le=1000)
    circuit_breaker_10m_duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    circuit_breaker_30m_duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    abnormal_volume_float_percent: float | None = Field(default=None, ge=.01, le=100)
    flow_concentration_percent: float | None = Field(default=None, ge=1, le=100)
    rapid_round_trip_percent: float | None = Field(default=None, ge=1, le=100)
    wash_round_trip_percent: float | None = Field(default=None, ge=1, le=100)
    coordinated_flow_imbalance_percent: float | None = Field(default=None, ge=1, le=100)
    coordinated_flow_min_participants: int | None = Field(default=None, ge=2, le=100)
    bankruptcy_watch_threshold: float | None = Field(default=None, ge=25, le=100)
    bankruptcy_ch11_threshold: float | None = Field(default=None, ge=50, le=100)
    bankruptcy_ch7_threshold: float | None = Field(default=None, ge=70, le=100)
    bankruptcy_ch7_loss_cycles: int | None = Field(default=None, ge=1, le=365)
    delisting_price_floor: float | None = Field(default=None, ge=.0001, le=1_000_000)
    events_enabled: bool | None = None
    bankruptcy_enabled: bool | None = None
    delisting_enabled: bool | None = None
    short_selling_enabled: bool | None = None
    halts_enabled: bool | None = None
    ipo_uncertainty_enabled: bool | None = None
    ipo_uncertainty_days: int | None = Field(default=None, ge=1, le=365)
    ipo_uncertainty_max_multiplier: float | None = Field(default=None, ge=1, le=10)
    paused_personalities: list[str] | None = None
    paused_tickers: list[str] | None = None
    distribution: dict[str, float] | None = None
    intervals: dict[str, int] | None = None


@app.get("/api/admin/snapshot", dependencies=[Depends(require_admin)])
def admin_market_snapshot() -> dict[str, Any]:
    with transaction() as db:
        result = admin_snapshot(db, load_settings(db))
    return {"ok": True, "engine": result}


@app.patch("/api/admin/settings", dependencies=[Depends(require_admin)])
def admin_settings(payload: EngineSettingsRequest) -> dict[str, Any]:
    values = payload.model_dump(exclude_none=True)
    direct = {
        "enabled": "fcx_engine_enabled", "kill_switch": "fcx_engine_kill_switch",
        "speed": "fcx_engine_speed", "random_seed": "fcx_engine_random_seed",
        "population": "fcx_engine_population", "total_capital": "fcx_engine_total_capital",
        "price_floor": "fcx_engine_price_floor", "minute_cap_percent": "fcx_engine_minute_cap_percent",
        "five_minute_cap_percent": "fcx_engine_five_minute_cap_percent",
        "thirty_minute_cap_percent": "fcx_engine_thirty_minute_cap_percent",
        "human_priority_percent": "fcx_engine_human_priority_percent",
        "max_order_percent": "fcx_engine_max_order_percent",
        "market_maker_spread_percent": "fcx_engine_market_maker_spread_percent",
        "market_maker_depth_multiplier": "fcx_engine_market_maker_depth_multiplier",
        "execution_budget_per_tick": "fcx_engine_execution_budget_per_tick",
        "panic_participation_percent": "fcx_engine_panic_participation_percent",
        "event_probability_percent": "fcx_engine_event_probability_percent",
        "sentiment_sensitivity": "fcx_engine_sentiment_sensitivity",
        "halt_risk_threshold": "fcx_engine_halt_risk_threshold",
        "circuit_breaker_10m_percent": "fcx_engine_circuit_breaker_10m_percent",
        "circuit_breaker_30m_percent": "fcx_engine_circuit_breaker_30m_percent",
        "circuit_breaker_10m_duration_minutes": "fcx_engine_circuit_breaker_10m_duration_minutes",
        "circuit_breaker_30m_duration_minutes": "fcx_engine_circuit_breaker_30m_duration_minutes",
        "abnormal_volume_float_percent": "fcx_engine_abnormal_volume_float_percent",
        "flow_concentration_percent": "fcx_engine_flow_concentration_percent",
        "rapid_round_trip_percent": "fcx_engine_rapid_round_trip_percent",
        "wash_round_trip_percent": "fcx_engine_wash_round_trip_percent",
        "coordinated_flow_imbalance_percent": "fcx_engine_coordinated_flow_imbalance_percent",
        "coordinated_flow_min_participants": "fcx_engine_coordinated_flow_min_participants",
        "bankruptcy_watch_threshold": "fcx_engine_bankruptcy_watch_threshold",
        "bankruptcy_ch11_threshold": "fcx_engine_bankruptcy_ch11_threshold",
        "bankruptcy_ch7_threshold": "fcx_engine_bankruptcy_ch7_threshold",
        "bankruptcy_ch7_loss_cycles": "fcx_engine_bankruptcy_ch7_loss_cycles",
        "delisting_price_floor": "fcx_engine_delisting_price_floor",
        "events_enabled": "fcx_engine_events_enabled", "bankruptcy_enabled": "fcx_engine_bankruptcy_enabled",
        "delisting_enabled": "fcx_engine_delisting_enabled",
        "short_selling_enabled": "fcx_engine_short_selling_enabled", "halts_enabled": "fcx_engine_halts_enabled",
        "ipo_uncertainty_enabled": "fcx_engine_ipo_uncertainty_enabled",
        "ipo_uncertainty_days": "fcx_engine_ipo_uncertainty_days",
        "ipo_uncertainty_max_multiplier": "fcx_engine_ipo_uncertainty_max_multiplier",
        "paused_personalities": "fcx_engine_paused_personalities", "paused_tickers": "fcx_engine_paused_tickers",
        "distribution": "fcx_engine_personality_distribution",
    }
    if "speed" in values and values["speed"] not in {"maintenance", "low", "normal", "high"}:
        raise HTTPException(status_code=400, detail="speed must be maintenance, low, normal, or high")
    intervals = values.pop("intervals", None)
    if intervals is not None:
        unknown = sorted(set(intervals) - set(CYCLE_DEFAULTS))
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unsupported interval keys: {', '.join(unknown)}")
        for cycle, seconds in intervals.items():
            if not 10 <= int(seconds) <= 604800:
                raise HTTPException(status_code=400, detail=f"{cycle} interval must be between 10 and 604800 seconds")
    with transaction() as db:
        for field, value in values.items():
            save_setting(db, direct[field], value)
        for cycle, seconds in (intervals or {}).items():
            save_setting(db, f"fcx_engine_interval_{cycle}_seconds", int(seconds))
        if values.get("enabled"):
            # Prevent the legacy PWA writers from racing the FCX clock.
            save_setting(db, "market_autopilot_enabled", False)
            save_setting(db, "market_gemini_autopilot_enabled", False)
        result = admin_snapshot(db, load_settings(db))
    return {"ok": True, "engine": result}


@app.post("/api/admin/seed", dependencies=[Depends(require_admin)])
def admin_seed(payload: SeedRequest) -> dict[str, Any]:
    if payload.replace and payload.confirmation.strip().upper() != "RESEED FCX":
        raise HTTPException(status_code=400, detail="Type RESEED FCX to replace the population")
    with transaction() as db:
        result = seed_investors(db, load_settings(db), replace=payload.replace)
    return {"ok": True, "result": result}


@app.post("/api/admin/cycle", dependencies=[Depends(require_admin)])
def admin_cycle(payload: CycleRequest) -> dict[str, Any]:
    if payload.cycle not in CYCLE_DEFAULTS:
        raise HTTPException(status_code=400, detail="Unsupported FCX cycle")
    with transaction() as db:
        result = run_manual_cycle(db, load_settings(db), payload.cycle)
    return {"ok": bool(result.get("ok")), "result": result}


@app.post("/api/admin/sandbox", dependencies=[Depends(require_admin)])
def admin_sandbox(payload: SandboxRequest) -> dict[str, Any]:
    seed = payload.seed if payload.seed is not None else 44217
    return {"ok": True, "result": run_sandbox(payload.days, seed)}


@app.post("/api/admin/corporate-actions/split", dependencies=[Depends(require_admin)])
def admin_stock_split(payload: StockSplitRequest) -> dict[str, Any]:
    if payload.confirmation.strip().upper() != "APPLY SPLIT":
        raise HTTPException(status_code=400, detail="Type APPLY SPLIT to authorize this corporate action")
    try:
        with transaction() as db:
            result = apply_stock_split(
                db, payload.ticker, payload.numerator, payload.denominator,
                actor_id=None, rationale=payload.rationale,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "result": result}


@app.post("/api/admin/corporate-actions/dividend", dependencies=[Depends(require_admin)])
def admin_dividend(payload: DividendRequest) -> dict[str, Any]:
    if payload.confirmation.strip().upper() != "DECLARE DIVIDEND":
        raise HTTPException(status_code=400, detail="Type DECLARE DIVIDEND to authorize this corporate action")
    try:
        with transaction() as db:
            result = apply_dividend(
                db, payload.ticker, payload.amount_per_share,
                actor_id=None, rationale=payload.rationale,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "result": result}


@app.post("/api/admin/kill-switch", dependencies=[Depends(require_admin)])
def admin_kill_switch(payload: KillSwitchRequest) -> dict[str, Any]:
    with transaction() as db:
        save_setting(db, "fcx_engine_kill_switch", payload.active)
        result = admin_snapshot(db, load_settings(db))
    return {"ok": True, "engine": result}


@app.post("/api/admin/market/pause", dependencies=[Depends(require_admin)])
def admin_market_pause() -> dict[str, Any]:
    with transaction() as db:
        save_setting(db, "fcx_engine_kill_switch", True)
        result = admin_snapshot(db, load_settings(db))
    return {"ok": True, "engine": result}


@app.post("/api/admin/market/resume", dependencies=[Depends(require_admin)])
def admin_market_resume() -> dict[str, Any]:
    with transaction() as db:
        save_setting(db, "fcx_engine_kill_switch", False)
        result = admin_snapshot(db, load_settings(db))
    return {"ok": True, "engine": result}


@app.post("/api/admin/ticker/{ticker}/pause", dependencies=[Depends(require_admin)])
def admin_ticker_pause(ticker: str) -> dict[str, Any]:
    return _set_ticker_pause(ticker, True)


@app.post("/api/admin/ticker/{ticker}/resume", dependencies=[Depends(require_admin)])
def admin_ticker_resume(ticker: str) -> dict[str, Any]:
    return _set_ticker_pause(ticker, False)
