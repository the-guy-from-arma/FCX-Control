from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .config import PERSONALITY_PROFILES


@dataclass(frozen=True)
class Decision:
    action: str
    score: float
    confidence: float
    reasons: tuple[str, ...]


def decide(personality: str, context: dict[str, Any], traits: dict[str, Any], rng: random.Random) -> Decision:
    profile = PERSONALITY_PROFILES.get(personality, PERSONALITY_PROFILES["retail"])
    momentum = float(context.get("momentum") or 0)
    valuation = float(context.get("valuation_gap") or 0)
    fundamental = float(context.get("fundamental_score") or 50) - 50
    sentiment = float(context.get("sentiment") or 50) - 50
    volatility = float(context.get("volatility") or 50)
    bankruptcy_risk = float(context.get("bankruptcy_risk") or 0)
    held_quantity = float(context.get("held_quantity") or 0)
    confidence_trait = float(traits.get("confidence") or 50)
    panic_threshold = float(traits.get("panic_threshold") or 30)

    components = {
        "momentum": momentum * profile["momentum"] * 0.70,
        "valuation": valuation * profile["fundamental"] * 0.65,
        "fundamentals": fundamental * profile["fundamental"] * 0.45,
        "sentiment": sentiment * profile["sentiment"] * 0.32,
        "risk": -(bankruptcy_risk * (1.0 - profile["risk"] / 100.0) * 0.65),
        "volatility": -(max(0.0, volatility - profile["risk"]) * 0.18),
    }
    if personality == "panic" and momentum < -panic_threshold / 4:
        components["panic"] = momentum * 1.3
    if personality == "contrarian" and momentum < -8 and bankruptcy_risk < 35:
        components["oversold"] = abs(momentum) * 1.15
    if personality == "short_seller":
        components["short thesis"] = -fundamental * 0.55 - valuation * 0.45
    if personality == "market_maker":
        components["liquidity provision"] = -momentum * 0.35
    score = sum(components.values()) + rng.uniform(-4.0, 4.0)
    score *= 0.72 + confidence_trait / 180.0

    if score >= 8:
        action = "ACCUMULATE" if held_quantity > 0 else "BUY"
    elif score <= -10 and held_quantity > 0:
        action = "LIQUIDATE" if score <= -28 else "SELL"
    elif score <= -14 and personality == "short_seller":
        action = "SHORT"
    elif held_quantity > 0 and score <= -4:
        action = "REDUCE"
    else:
        action = "HOLD"

    ranked = sorted(components.items(), key=lambda item: abs(item[1]), reverse=True)
    reasons = tuple(
        f"{label.replace('_', ' ').title()} {'supported' if value >= 0 else 'opposed'} the position ({value:+.1f})"
        for label, value in ranked[:4]
    )
    confidence = max(1.0, min(99.0, 50.0 + abs(score) * 1.4 + (confidence_trait - 50) * 0.25))
    return Decision(action, round(score, 3), round(confidence, 2), reasons)
