from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any


WEIGHT_QUANTUM = Decimal("0.00000001")


def security_market_cap(row: dict[str, Any]) -> Decimal:
    price = max(Decimal("0"), Decimal(str(row.get("price") or 0)))
    shares = max(Decimal("0"), Decimal(str(row.get("issued_shares") or 0)))
    return price * shares


def rank_by_market_cap(rows: list[dict[str, Any]], target_size: int) -> list[dict[str, Any]]:
    limit = max(0, int(target_size))
    return sorted(
        rows,
        key=lambda row: (security_market_cap(row), str(row.get("ticker") or "")),
        reverse=True,
    )[:limit]


def market_cap_weights(rows: list[dict[str, Any]]) -> list[Decimal]:
    if not rows:
        return []
    caps = [security_market_cap(row) for row in rows]
    total = sum(caps, Decimal("0"))
    raw = [cap / total for cap in caps] if total > 0 else [Decimal("1") / Decimal(len(rows)) for _ in rows]
    weights: list[Decimal] = []
    allocated = Decimal("0")
    for index, value in enumerate(raw):
        weight = Decimal("1") - allocated if index == len(raw) - 1 else value.quantize(WEIGHT_QUANTUM, rounding=ROUND_HALF_UP)
        weights.append(weight)
        allocated += weight
    return weights
