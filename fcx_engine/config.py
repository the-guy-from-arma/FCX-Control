from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


PERSONALITY_PROFILES: dict[str, dict[str, float]] = {
    "retail": {"risk": 50, "frequency": 0.52, "size": 0.018, "reserve": 0.20, "momentum": 0.45, "fundamental": 0.35, "sentiment": 0.55},
    "conservative": {"risk": 28, "frequency": 0.18, "size": 0.035, "reserve": 0.34, "momentum": 0.15, "fundamental": 0.90, "sentiment": 0.18},
    "growth": {"risk": 66, "frequency": 0.38, "size": 0.055, "reserve": 0.15, "momentum": 0.55, "fundamental": 0.78, "sentiment": 0.35},
    "panic": {"risk": 48, "frequency": 0.85, "size": 0.030, "reserve": 0.16, "momentum": 0.82, "fundamental": 0.08, "sentiment": 0.95},
    "contrarian": {"risk": 60, "frequency": 0.34, "size": 0.048, "reserve": 0.18, "momentum": -0.72, "fundamental": 0.75, "sentiment": -0.35},
    "institutional": {"risk": 44, "frequency": 0.16, "size": 0.095, "reserve": 0.22, "momentum": 0.18, "fundamental": 0.82, "sentiment": 0.18},
    "momentum": {"risk": 68, "frequency": 0.70, "size": 0.042, "reserve": 0.12, "momentum": 0.96, "fundamental": 0.18, "sentiment": 0.48},
    "value": {"risk": 46, "frequency": 0.28, "size": 0.050, "reserve": 0.24, "momentum": -0.18, "fundamental": 0.96, "sentiment": 0.12},
    "day_trader": {"risk": 72, "frequency": 0.92, "size": 0.024, "reserve": 0.10, "momentum": 0.88, "fundamental": 0.06, "sentiment": 0.40},
    "speculator": {"risk": 88, "frequency": 0.68, "size": 0.065, "reserve": 0.07, "momentum": 0.74, "fundamental": -0.22, "sentiment": 0.72},
    "dividend": {"risk": 30, "frequency": 0.12, "size": 0.045, "reserve": 0.30, "momentum": 0.04, "fundamental": 0.92, "sentiment": 0.10},
    "short_seller": {"risk": 74, "frequency": 0.44, "size": 0.036, "reserve": 0.25, "momentum": -0.72, "fundamental": -0.58, "sentiment": -0.38},
    "market_maker": {"risk": 38, "frequency": 0.98, "size": 0.020, "reserve": 0.32, "momentum": -0.10, "fundamental": 0.06, "sentiment": -0.08},
    "whale": {"risk": 64, "frequency": 0.10, "size": 0.125, "reserve": 0.18, "momentum": 0.28, "fundamental": 0.55, "sentiment": 0.32},
    "algorithmic": {"risk": 58, "frequency": 0.82, "size": 0.027, "reserve": 0.16, "momentum": 0.78, "fundamental": 0.26, "sentiment": 0.08},
}

DEFAULT_DISTRIBUTION: dict[str, float] = {
    "retail": 30.0,
    "conservative": 10.0,
    "growth": 10.0,
    "momentum": 10.0,
    "day_trader": 8.0,
    "value": 8.0,
    "contrarian": 6.0,
    "speculator": 5.0,
    "dividend": 4.0,
    "short_seller": 3.0,
    "institutional": 2.0,
    "market_maker": 2.0,
    "algorithmic": 1.5,
    "whale": 0.5,
    "panic": 0.0,
}

CYCLE_DEFAULTS = {
    "minute": 60,
    "five_minute": 300,
    "fifteen_minute": 900,
    "thirty_minute": 1800,
    "hourly": 3600,
    "six_hour": 21600,
    "daily": 86400,
}

SPEED_MULTIPLIERS = {"maintenance": 0.0, "low": 0.30, "normal": 1.0, "high": 1.50}


def _truthy(value: Any) -> bool:
    return str(value or "0").strip().lower() in {"1", "true", "yes", "on"}


def _number(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def parse_string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        raw = value
    else:
        try:
            raw = json.loads(str(value or "[]"))
        except (TypeError, json.JSONDecodeError):
            raw = []
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({str(item).strip().lower() for item in raw if str(item).strip()}))


def parse_distribution(value: Any) -> dict[str, float]:
    if isinstance(value, dict):
        raw = value
    else:
        try:
            raw = json.loads(str(value or "{}"))
        except (TypeError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    cleaned = {
        key: _number(raw.get(key, DEFAULT_DISTRIBUTION.get(key, 0)), 0, 0, 100)
        for key in PERSONALITY_PROFILES
    }
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_DISTRIBUTION)
    return {key: round(value / total * 100.0, 4) for key, value in cleaned.items()}


@dataclass(frozen=True)
class EngineConfig:
    enabled: bool
    kill_switch: bool
    speed: str
    random_seed: int
    population: int
    total_capital: float
    price_floor: float
    minute_cap_percent: float
    five_minute_cap_percent: float
    thirty_minute_cap_percent: float
    human_priority: float
    max_order_percent: float
    market_maker_spread_percent: float
    market_maker_depth_multiplier: float
    execution_budget_per_tick: int
    panic_participation_percent: float
    event_probability_percent: float
    sentiment_sensitivity: float
    company_distress_threshold: float
    halt_risk_threshold: float
    circuit_breaker_10m_percent: float
    circuit_breaker_30m_percent: float
    circuit_breaker_10m_duration_minutes: int
    circuit_breaker_30m_duration_minutes: int
    abnormal_volume_float_percent: float
    flow_concentration_percent: float
    rapid_round_trip_percent: float
    wash_round_trip_percent: float
    coordinated_flow_imbalance_percent: float
    coordinated_flow_min_participants: int
    bankruptcy_watch_threshold: float
    bankruptcy_ch11_threshold: float
    bankruptcy_ch7_threshold: float
    bankruptcy_ch7_loss_cycles: int
    delisting_price_floor: float
    ipo_uncertainty_enabled: bool
    ipo_uncertainty_days: int
    ipo_uncertainty_max_multiplier: float
    events_enabled: bool
    bankruptcy_enabled: bool
    delisting_enabled: bool
    short_selling_enabled: bool
    halt_enabled: bool
    paused_personalities: tuple[str, ...]
    paused_tickers: tuple[str, ...]
    distribution: dict[str, float]
    intervals: dict[str, int]

    @property
    def activity_multiplier(self) -> float:
        return SPEED_MULTIPLIERS.get(self.speed, 1.0)

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "EngineConfig":
        speed = str(settings.get("fcx_engine_speed") or "normal").strip().lower()
        if speed not in SPEED_MULTIPLIERS:
            speed = "normal"
        intervals = {
            name: int(_number(settings.get(f"fcx_engine_interval_{name}_seconds"), default, 10, 604800))
            for name, default in CYCLE_DEFAULTS.items()
        }
        return cls(
            enabled=_truthy(settings.get("fcx_engine_enabled")),
            kill_switch=_truthy(settings.get("fcx_engine_kill_switch")),
            speed=speed,
            random_seed=int(_number(settings.get("fcx_engine_random_seed"), 44217, 1, 2147483647)),
            population=int(_number(settings.get("fcx_engine_population"), 250, 1, 5000)),
            total_capital=_number(settings.get("fcx_engine_total_capital"), 250000000, 1000, 1000000000000),
            price_floor=_number(settings.get("fcx_engine_price_floor"), 0.01, 0.0001, 1000000),
            minute_cap_percent=_number(settings.get("fcx_engine_minute_cap_percent"), 2, 0.01, 100),
            five_minute_cap_percent=_number(settings.get("fcx_engine_five_minute_cap_percent"), 5, 0.01, 200),
            thirty_minute_cap_percent=_number(settings.get("fcx_engine_thirty_minute_cap_percent"), 15, 0.01, 500),
            human_priority=_number(settings.get("fcx_engine_human_priority_percent"), 70, 0, 100) / 100.0,
            max_order_percent=_number(settings.get("fcx_engine_max_order_percent"), 5, 0.01, 50) / 100.0,
            market_maker_spread_percent=_number(settings.get("fcx_engine_market_maker_spread_percent"), 0.35, 0.01, 25),
            market_maker_depth_multiplier=_number(settings.get("fcx_engine_market_maker_depth_multiplier"), 1, 0.1, 10),
            execution_budget_per_tick=int(_number(settings.get("fcx_engine_execution_budget_per_tick"), 80, 10, 5000)),
            panic_participation_percent=_number(settings.get("fcx_engine_panic_participation_percent"), 20, 0, 100),
            event_probability_percent=_number(settings.get("fcx_engine_event_probability_percent"), 30, 0, 100),
            sentiment_sensitivity=_number(settings.get("fcx_engine_sentiment_sensitivity"), 1, 0, 5),
            company_distress_threshold=_number(settings.get("fcx_engine_company_distress_threshold"), 75, 50, 100),
            halt_risk_threshold=_number(settings.get("fcx_engine_halt_risk_threshold"), 95, 50, 100),
            circuit_breaker_10m_percent=_number(settings.get("fcx_engine_circuit_breaker_10m_percent"), 20, 0.1, 500),
            circuit_breaker_30m_percent=_number(settings.get("fcx_engine_circuit_breaker_30m_percent"), 35, 0.1, 1000),
            circuit_breaker_10m_duration_minutes=int(_number(settings.get("fcx_engine_circuit_breaker_10m_duration_minutes"), 15, 1, 1440)),
            circuit_breaker_30m_duration_minutes=int(_number(settings.get("fcx_engine_circuit_breaker_30m_duration_minutes"), 30, 1, 1440)),
            abnormal_volume_float_percent=_number(settings.get("fcx_engine_abnormal_volume_float_percent"), 5, 0.01, 100),
            flow_concentration_percent=_number(settings.get("fcx_engine_flow_concentration_percent"), 75, 1, 100),
            rapid_round_trip_percent=_number(settings.get("fcx_engine_rapid_round_trip_percent"), 65, 1, 100),
            wash_round_trip_percent=_number(settings.get("fcx_engine_wash_round_trip_percent"), 85, 1, 100),
            coordinated_flow_imbalance_percent=_number(settings.get("fcx_engine_coordinated_flow_imbalance_percent"), 80, 1, 100),
            coordinated_flow_min_participants=int(_number(settings.get("fcx_engine_coordinated_flow_min_participants"), 4, 2, 100)),
            bankruptcy_watch_threshold=_number(settings.get("fcx_engine_bankruptcy_watch_threshold"), 70, 25, 100),
            bankruptcy_ch11_threshold=_number(settings.get("fcx_engine_bankruptcy_ch11_threshold"), 92, 50, 100),
            bankruptcy_ch7_threshold=_number(settings.get("fcx_engine_bankruptcy_ch7_threshold"), 99, 70, 100),
            bankruptcy_ch7_loss_cycles=int(_number(settings.get("fcx_engine_bankruptcy_ch7_loss_cycles"), 6, 1, 365)),
            delisting_price_floor=_number(settings.get("fcx_engine_delisting_price_floor"), 0.05, 0.0001, 1000000),
            ipo_uncertainty_enabled=_truthy(settings.get("fcx_engine_ipo_uncertainty_enabled", 1)),
            ipo_uncertainty_days=int(_number(settings.get("fcx_engine_ipo_uncertainty_days"), 7, 1, 90)),
            ipo_uncertainty_max_multiplier=_number(settings.get("fcx_engine_ipo_uncertainty_max_multiplier"), 1.75, 1, 5),
            events_enabled=_truthy(settings.get("fcx_engine_events_enabled", 1)),
            bankruptcy_enabled=_truthy(settings.get("fcx_engine_bankruptcy_enabled")),
            delisting_enabled=_truthy(settings.get("fcx_engine_delisting_enabled")),
            short_selling_enabled=_truthy(settings.get("fcx_engine_short_selling_enabled", 1)),
            halt_enabled=_truthy(settings.get("fcx_engine_halts_enabled", 1)),
            paused_personalities=parse_string_list(settings.get("fcx_engine_paused_personalities")),
            paused_tickers=parse_string_list(settings.get("fcx_engine_paused_tickers")),
            distribution=parse_distribution(settings.get("fcx_engine_personality_distribution")),
            intervals=intervals,
        )
