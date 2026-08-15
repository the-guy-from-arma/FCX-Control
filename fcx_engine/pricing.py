from __future__ import annotations

import math
import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class PriceDiscovery:
    old_price: float
    new_price: float
    movement_percent: float
    raw_signal: float
    liquidity: float
    explanation: tuple[str, ...]


@dataclass(frozen=True)
class MarketMakerQuote:
    mid_price: float
    bid_price: float
    ask_price: float
    spread_percent: float
    bid_depth: float
    ask_depth: float


def market_maker_quote(
    *,
    price: float,
    volatility: float,
    configured_spread_percent: float,
    issued_shares: float,
    provider_count: int,
    depth_factor: float = 1.0,
    price_floor: float = 0.01,
) -> MarketMakerQuote:
    """Build a deterministic, bounded indicative quote around the live price.

    This quote book is telemetry for the FCX engine and its operator console;
    it never becomes a second resident order ledger or bypasses Ravenhood's
    existing execution path.
    """
    mid = max(float(price_floor), float(price or price_floor))
    providers = max(0, int(provider_count or 0))
    volatility_scale = 1.0 + max(0.0, min(100.0, float(volatility or 0.0))) / 285.0
    spread = max(0.01, min(25.0, float(configured_spread_percent or 0.35) * volatility_scale))
    half_spread = spread / 200.0
    bid = max(float(price_floor), mid * (1.0 - half_spread))
    ask = max(bid + float(price_floor) * 0.0001, mid * (1.0 + half_spread))
    base_depth = max(1.0, float(issued_shares or 1.0) * 0.00025)
    depth = base_depth * max(1, providers) * max(0.25, min(2.0, float(depth_factor or 1.0)))
    return MarketMakerQuote(
        mid_price=round(mid, 4),
        bid_price=round(bid, 4),
        ask_price=round(ask, 4),
        spread_percent=round(spread, 4),
        bid_depth=round(depth * 1.03, 6),
        ask_depth=round(depth * 0.97, 6),
    )


def discover_price(
    *,
    price: float,
    human_buy: float,
    human_sell: float,
    npc_buy: float,
    npc_sell: float,
    issued_shares: float,
    volatility: float,
    market_sentiment: float,
    company_sentiment: float,
    fundamental_score: float,
    fair_value: float,
    cap_percent: float,
    price_floor: float,
) -> PriceDiscovery:
    safe_price = max(price_floor, float(price or price_floor))
    float_value = max(safe_price * max(issued_shares, 1.0), safe_price)
    gross_volume = max(0.0, human_buy + human_sell + npc_buy + npc_sell)
    net_volume = human_buy + npc_buy - human_sell - npc_sell
    liquidity = max(1.0, math.sqrt(float_value) * (1.0 + math.log10(1.0 + gross_volume)))
    pressure = net_volume / liquidity * 100.0
    sentiment_signal = ((market_sentiment - 50.0) * 0.018) + ((company_sentiment - 50.0) * 0.024)
    fundamental_signal = (fundamental_score - 50.0) * 0.012
    valuation_signal = 0.0
    if fair_value > 0:
        valuation_signal = max(-4.0, min(4.0, (fair_value - safe_price) / safe_price * 1.8))
    volatility_scale = 0.45 + max(0.0, min(100.0, volatility)) / 100.0
    raw = (pressure + sentiment_signal + fundamental_signal + valuation_signal) * volatility_scale
    bounded = max(-abs(cap_percent), min(abs(cap_percent), raw))
    new_price = max(price_floor, safe_price * (1.0 + bounded / 100.0))
    if not math.isfinite(new_price):
        new_price = safe_price
        bounded = 0.0
    return PriceDiscovery(
        round(safe_price, 4),
        round(new_price, 4),
        round(bounded, 4),
        round(raw, 4),
        round(liquidity, 2),
        (
            f"Net order pressure {net_volume:+,.2f} shares",
            f"Liquidity denominator {liquidity:,.2f}",
            f"Market/company sentiment {market_sentiment:.1f}/{company_sentiment:.1f}",
            f"Fundamental score {fundamental_score:.1f}",
        ),
    )


def fear_greed(momentum: float, volatility: float, breadth: float, sentiment: float, speculative_share: float) -> float:
    score = 50.0 + momentum * 0.55 + (breadth - 50.0) * 0.28 + (sentiment - 50.0) * 0.42
    score -= max(0.0, volatility - 55.0) * 0.32
    score += (speculative_share - 15.0) * 0.12
    return round(max(0.0, min(100.0, score)), 2)


def regime_for(sentiment: float, volatility: float, momentum: float) -> str:
    if volatility >= 85 and sentiment <= 25:
        return "crisis"
    if volatility >= 70:
        return "high_volatility"
    if momentum >= 4 and sentiment >= 58:
        return "bull"
    if momentum <= -4 and sentiment <= 42:
        return "bear"
    if momentum >= 1 and sentiment >= 48:
        return "recovery"
    return "sideways"


def index_nav(base_nav: float, members: list[tuple[float, float, float]], price_floor: float = 0.01) -> float:
    """Return the constituent-weighted NAV used by the existing FCXS/FCXV funds.

    Each member tuple is ``(weight, current_price, reference_price)``.  The
    helper is intentionally pure so the autonomous worker and accelerated
    sandbox can use the same deterministic calculation without owning resident
    fund holdings or creating a second index-pricing system.
    """
    safe_base = max(float(price_floor), float(base_nav or price_floor))
    performance = 0.0
    for weight, current_price, reference_price in members:
        safe_weight = max(0.0, float(weight or 0.0))
        safe_reference = max(float(price_floor), float(reference_price or price_floor))
        safe_current = max(float(price_floor), float(current_price or price_floor))
        if safe_weight <= 0:
            continue
        performance += safe_weight * (safe_current / safe_reference)
    if performance <= 0:
        return round(safe_base, 4)
    # This intentionally matches Ravenhood's established index calculation.
    # Rebalances are responsible for storing weights that sum to one.
    value = safe_base * performance
    if not math.isfinite(value):
        value = safe_base
    return round(max(float(price_floor), value), 4)


def short_squeeze_cover_quantity(
    short_quantity: float,
    issued_shares: float,
    momentum_percent: float,
    volatility: float,
) -> float:
    """Return a bounded forced-cover quantity for an overcrowded short trade.

    A squeeze requires both meaningful short interest and an advancing price.
    The result is deliberately capped at 35% of open short interest per minute
    so a squeeze creates sustained buy pressure instead of a one-tick spike.
    """
    open_short = max(0.0, float(short_quantity or 0.0))
    float_shares = max(1.0, float(issued_shares or 1.0))
    momentum = float(momentum_percent or 0.0)
    if open_short <= 0 or momentum < 3.0:
        return 0.0
    short_interest_percent = open_short / float_shares * 100.0
    if short_interest_percent < 8.0:
        return 0.0
    stress = (
        (short_interest_percent - 8.0) / 100.0
        + momentum / 100.0
        + max(0.0, float(volatility or 0.0) - 50.0) / 500.0
    )
    fraction = max(0.05, min(0.35, stress))
    return round(min(open_short, open_short * fraction), 6)


def ipo_uncertainty_multiplier(
    activated_at: str | None,
    now: dt.datetime,
    window_days: int = 7,
    maximum_multiplier: float = 1.75,
) -> float:
    """Return a decaying volatility multiplier for a newly listed company.

    The value begins at ``maximum_multiplier`` and decays linearly to 1.0 over
    the configured window. Missing or future timestamps do not make the engine
    unsafe: a missing timestamp is treated as established, while a future
    timestamp receives the maximum uncertainty.
    """
    if not activated_at:
        return 1.0
    try:
        listed_at = dt.datetime.fromisoformat(str(activated_at).replace("Z", "+00:00"))
        if listed_at.tzinfo is None:
            listed_at = listed_at.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return 1.0
    reference = now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
    duration = max(1.0, float(window_days) * 86400.0)
    age = max(0.0, (reference - listed_at).total_seconds())
    remaining = max(0.0, min(1.0, 1.0 - age / duration))
    return round(1.0 + (max(1.0, float(maximum_multiplier)) - 1.0) * remaining, 6)


def split_adjustment(quantity: float, price: float, numerator: float, denominator: float) -> tuple[float, float]:
    """Return quantity and price after a forward or reverse stock split."""
    top = float(numerator)
    bottom = float(denominator)
    if top <= 0 or bottom <= 0:
        raise ValueError("Stock split ratios must be positive")
    ratio = top / bottom
    new_quantity = max(0.0, float(quantity or 0.0) * ratio)
    new_price = max(0.0, float(price or 0.0) / ratio)
    if not math.isfinite(new_quantity) or not math.isfinite(new_price):
        raise ValueError("Stock split result is not finite")
    return round(new_quantity, 8), round(new_price, 4)
