from __future__ import annotations

import math
import random
from typing import Any


def run_sandbox(days: int, seed: int = 44217, profile: str = "normal") -> dict[str, Any]:
    days = max(1, min(365, int(days)))
    rng = random.Random(int(seed))
    speed = {"low": 0.55, "normal": 1.0, "high": 1.45, "maintenance": 0.0}.get(str(profile), 1.0)
    index_value = 1000.0
    starting = index_value
    peak = index_value
    drawdown = 0.0
    changes: list[float] = []
    halts = bankruptcies = delistings = trades = 0
    volume = 0.0
    points: list[dict[str, Any]] = []
    for day in range(1, days + 1):
        fundamental = math.sin(day / 29.0) * 0.45
        momentum = (sum(changes[-5:]) / max(1, len(changes[-5:]))) * 0.18
        shock = rng.gauss(0, 0.72 * speed)
        if rng.random() < 0.006 * speed:
            shock += rng.choice([-1, 1]) * rng.uniform(2.0, 6.5)
        change = max(-12.0, min(12.0, fundamental + momentum + shock))
        index_value = max(50.0, index_value * (1 + change / 100.0))
        peak = max(peak, index_value)
        drawdown = max(drawdown, (peak - index_value) / peak * 100.0)
        changes.append(change)
        daily_trades = int((180 + rng.randint(-45, 80)) * speed)
        trades += daily_trades
        volume += daily_trades * rng.uniform(1200, 18000)
        if abs(change) >= 8:
            halts += rng.randint(1, 3)
        if rng.random() < 0.0008 * speed:
            bankruptcies += 1
        if rng.random() < 0.0012 * speed:
            delistings += 1
        if days <= 30 or day == 1 or day == days or day % max(1, days // 30) == 0:
            points.append({"day": day, "index": round(index_value, 2), "change_percent": round(change, 3)})
    largest_gain = max(changes) if changes else 0
    largest_loss = min(changes) if changes else 0
    mean = sum(changes) / max(1, len(changes))
    variance = sum((item - mean) ** 2 for item in changes) / max(1, len(changes))
    return {
        "days": days,
        "seed": int(seed),
        "profile": profile,
        "starting_fcx": round(starting, 2),
        "ending_fcx": round(index_value, 2),
        "change_percent": round((index_value / starting - 1) * 100, 2),
        "largest_gain_percent": round(largest_gain, 2),
        "largest_loss_percent": round(largest_loss, 2),
        "bankruptcies": bankruptcies,
        "delistings": delistings,
        "halts": halts,
        "npc_trades": trades,
        "total_volume": round(volume, 2),
        "maximum_drawdown_percent": round(drawdown, 2),
        "realized_volatility": round(math.sqrt(variance), 3),
        "npc_capital_inflation_percent": 0.0,
        "points": points,
    }
