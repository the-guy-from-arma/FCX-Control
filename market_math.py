from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any


def ravenhood_security_session_open(
    market_open: bool, ticker: str, fcxv_24h_enabled: bool
) -> bool:
    """Return whether a Ravenhood security may trade right now.

    FCXV is the only continuous-session security. The same policy applies to
    ordinary shares and leveraged long/short positions.
    """
    return bool(market_open) or (
        bool(fcxv_24h_enabled) and str(ticker or "").strip().upper() == "FCXV"
    )


def market_cap_weighted_allocations(
    capitalizations: list[tuple[int, Any]], total_amount: Any
) -> list[dict[str, float | int]]:
    """Allocate a currency amount across securities by market capitalization.

    Every returned allocation is rounded down to cents.  Any remaining cents
    are assigned to the final eligible security so the allocations always
    reconcile exactly to the requested custody-pool disposition.
    """
    try:
        total = Decimal(str(total_amount or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("total_amount_must_be_numeric") from exc
    if total <= 0:
        raise ValueError("total_amount_must_be_positive")

    eligible: list[tuple[int, Decimal]] = []
    for security_id, raw_cap in capitalizations:
        try:
            cap = Decimal(str(raw_cap or 0))
        except (InvalidOperation, ValueError):
            continue
        if int(security_id) > 0 and cap > 0:
            eligible.append((int(security_id), cap))
    total_cap = sum((cap for _, cap in eligible), Decimal("0"))
    if total_cap <= 0:
        raise ValueError("no_positive_market_capitalizations")

    remaining = total
    allocations: list[dict[str, float | int]] = []
    for index, (security_id, cap) in enumerate(eligible):
        amount = (
            remaining
            if index == len(eligible) - 1
            else (total * cap / total_cap).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        )
        remaining -= amount
        allocations.append(
            {
                "security_id": security_id,
                "amount": float(amount),
                "weight": float(cap / total_cap),
            }
        )
    return allocations


def market_gemini_exposure_shares(buy_shares: Any, sell_shares: Any) -> float:
    """Return Gemini's directional constituent exposure in share units.

    Gemini-generated market trades are intentionally two-sided. Stability
    names often finish a cycle buy-heavy, while volatility names can finish
    sell-heavy. Treating a sell-heavy position as zero made FCXV appear to
    have no Gemini participation even though its constituents had active
    Gemini flow. Fund capitalization uses the magnitude of that net exposure;
    the direction remains available separately in the constituent payload.
    """
    bought = max(0.0, float(buy_shares or 0))
    sold = max(0.0, float(sell_shares or 0))
    return abs(bought - sold)


def ravenhood_residential_pnl_windows(
    equity_trades: list[dict[str, Any]],
    current_holdings: list[dict[str, Any]],
    margin_positions: list[dict[str, Any]],
    cutoff_prices: dict[str, dict[int, Any]],
    windows: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build resident P&L surveillance summaries for several lookback windows.

    Cash-equity P&L uses the quote at the beginning of each window as the
    baseline for shares already held, then applies actual execution prices and
    fees to buys and sells inside the window. Open leveraged positions are
    marked to the current quote; closed positions use their recorded close.
    The function never includes system-market-maker rows because every input
    record is expected to belong to a resident Ravenhood account.
    """

    def number(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def timestamp(value: Any) -> dt.datetime:
        if isinstance(value, dt.datetime):
            parsed = value
        else:
            text = str(value or "").strip().replace("Z", "+00:00")
            try:
                parsed = dt.datetime.fromisoformat(text)
            except ValueError:
                parsed = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)

    holding_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    identity_by_account: dict[int, dict[str, Any]] = {}
    for raw in current_holdings:
        row = dict(raw)
        account_id = int(number(row.get("account_id")))
        security_id = int(number(row.get("security_id")))
        if account_id <= 0 or security_id <= 0:
            continue
        holding_by_key[(account_id, security_id)] = row
        identity_by_account[account_id] = row

    equity_by_key: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for raw in equity_trades:
        row = dict(raw)
        account_id = int(number(row.get("account_id")))
        security_id = int(number(row.get("security_id")))
        if account_id <= 0 or security_id <= 0:
            continue
        equity_by_key[(account_id, security_id)].append(row)
        identity_by_account[account_id] = row
    for rows in equity_by_key.values():
        rows.sort(key=lambda row: timestamp(row.get("created_at")))

    for raw in margin_positions:
        row = dict(raw)
        account_id = int(number(row.get("account_id")))
        if account_id > 0:
            identity_by_account[account_id] = row

    results: dict[str, dict[str, Any]] = {}
    all_equity_keys = set(holding_by_key) | set(equity_by_key)
    for window_key, cutoff_value in windows.items():
        cutoff = timestamp(cutoff_value)
        window_prices = cutoff_prices.get(window_key, {})
        residents: dict[int, dict[str, Any]] = {}

        def resident(account_id: int) -> dict[str, Any]:
            if account_id not in residents:
                identity = identity_by_account.get(account_id, {})
                residents[account_id] = {
                    "account_id": account_id,
                    "user_id": int(number(identity.get("user_id"))),
                    "name": str(identity.get("name") or identity.get("resident_name") or "Resident"),
                    "civ_number": str(identity.get("civ_number") or "pending"),
                    "equity_realized": 0.0,
                    "equity_unrealized": 0.0,
                    "margin_realized": 0.0,
                    "margin_unrealized": 0.0,
                }
            return residents[account_id]

        for account_id, security_id in all_equity_keys:
            holding = holding_by_key.get((account_id, security_id), {})
            current_quantity = max(0.0, number(holding.get("quantity")))
            mark_price = max(
                0.0,
                number(holding.get("current_price") or holding.get("mark_price")),
            )
            trades = [
                row for row in equity_by_key.get((account_id, security_id), [])
                if timestamp(row.get("created_at")) >= cutoff
            ]
            if not trades and current_quantity <= 0:
                continue

            cutoff_price = number(window_prices.get(security_id))
            if cutoff_price <= 0:
                cutoff_price = number(trades[0].get("unit_price")) if trades else mark_price
            if cutoff_price <= 0:
                cutoff_price = mark_price

            buys = sum(number(row.get("quantity")) for row in trades if str(row.get("action") or row.get("side") or "").lower() == "buy")
            sells = sum(number(row.get("quantity")) for row in trades if str(row.get("action") or row.get("side") or "").lower() == "sell")
            opening_quantity = max(0.0, current_quantity - buys + sells)
            tracked_quantity = opening_quantity
            tracked_basis = opening_quantity * cutoff_price
            equity_realized = 0.0

            for row in trades:
                side = str(row.get("action") or row.get("side") or "").lower()
                quantity = max(0.0, number(row.get("quantity")))
                unit_price = max(0.0, number(row.get("unit_price")))
                fee = max(0.0, number(row.get("fee_amount")))
                gross = max(0.0, number(row.get("gross_amount"))) or quantity * unit_price
                if side == "buy":
                    tracked_quantity += quantity
                    tracked_basis += gross + fee
                elif side == "sell" and quantity > 0:
                    average_basis = tracked_basis / tracked_quantity if tracked_quantity > 0 else cutoff_price
                    equity_realized += gross - fee - (average_basis * quantity)
                    removed = min(quantity, tracked_quantity)
                    tracked_basis = max(0.0, tracked_basis - average_basis * removed)
                    tracked_quantity = max(0.0, tracked_quantity - removed)

            if abs(tracked_quantity - current_quantity) > 0.000001:
                average_basis = tracked_basis / tracked_quantity if tracked_quantity > 0 else cutoff_price
                tracked_quantity = current_quantity
                tracked_basis = max(0.0, average_basis * current_quantity)
            equity_unrealized = current_quantity * mark_price - tracked_basis
            account = resident(account_id)
            account["equity_realized"] += equity_realized
            account["equity_unrealized"] += equity_unrealized

        for raw in margin_positions:
            row = dict(raw)
            account_id = int(number(row.get("account_id")))
            security_id = int(number(row.get("security_id")))
            if account_id <= 0 or security_id <= 0:
                continue
            opened_at = timestamp(row.get("opened_at") or row.get("created_at"))
            status = str(row.get("status") or "open").lower()
            closed_at = timestamp(row.get("closed_at")) if row.get("closed_at") else None
            if status != "open" and (closed_at is None or closed_at < cutoff):
                continue
            direction = str(row.get("direction") or row.get("action") or "long").lower()
            quantity = max(0.0, number(row.get("quantity")))
            entry_price = max(0.0, number(row.get("entry_price") or row.get("unit_price")))
            mark_price = max(0.0, number(row.get("mark_price")))
            cutoff_price = number(window_prices.get(security_id))
            baseline = entry_price if opened_at >= cutoff else (cutoff_price if cutoff_price > 0 else entry_price)
            open_fee = max(0.0, number(row.get("open_fee") or row.get("fee_amount"))) if opened_at >= cutoff else 0.0
            account = resident(account_id)
            if status == "open":
                raw_pnl = quantity * (mark_price - baseline) if direction == "long" else quantity * (baseline - mark_price)
                account["margin_unrealized"] += raw_pnl - open_fee
            else:
                close_price = max(0.0, number(row.get("close_price")))
                raw_pnl = quantity * (close_price - baseline) if direction == "long" else quantity * (baseline - close_price)
                account["margin_realized"] += raw_pnl - open_fee - max(0.0, number(row.get("close_fee")))

        resident_rows: list[dict[str, Any]] = []
        for account in residents.values():
            account["realized_pnl"] = round(account["equity_realized"] + account["margin_realized"], 2)
            account["unrealized_pnl"] = round(account["equity_unrealized"] + account["margin_unrealized"], 2)
            account["net_pnl"] = round(account["realized_pnl"] + account["unrealized_pnl"], 2)
            for field in ("equity_realized", "equity_unrealized", "margin_realized", "margin_unrealized"):
                account[field] = round(account[field], 2)
            resident_rows.append(account)
        resident_rows.sort(key=lambda row: abs(number(row.get("net_pnl"))), reverse=True)

        realized = round(sum(number(row.get("realized_pnl")) for row in resident_rows), 2)
        unrealized = round(sum(number(row.get("unrealized_pnl")) for row in resident_rows), 2)
        net = round(realized + unrealized, 2)
        results[window_key] = {
            "cutoff_at": cutoff.isoformat(),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "net_pnl": net,
            "equity_pnl": round(sum(number(row.get("equity_realized")) + number(row.get("equity_unrealized")) for row in resident_rows), 2),
            "margin_pnl": round(sum(number(row.get("margin_realized")) + number(row.get("margin_unrealized")) for row in resident_rows), 2),
            "resident_count": len(resident_rows),
            "profitable_residents": sum(1 for row in resident_rows if number(row.get("net_pnl")) > 0.005),
            "losing_residents": sum(1 for row in resident_rows if number(row.get("net_pnl")) < -0.005),
            "flat_residents": sum(1 for row in resident_rows if abs(number(row.get("net_pnl"))) <= 0.005),
            "residents": resident_rows,
        }
    return results


def ravenhood_margin_quote(
    direction: str,
    entry_price: Any,
    collateral: Any,
    leverage: Any,
    fee_percent: Any,
    maintenance_ratio: Any,
) -> dict[str, float]:
    """Build a deterministic isolated-margin opening quote.

    The position notional is collateral multiplied by leverage.  Maintenance
    is expressed as a fraction of the original collateral, so a 20% setting
    liquidates a position when only 20% of its isolated collateral remains.
    """
    normalized_direction = str(direction or "").strip().lower()
    if normalized_direction not in {"long", "short"}:
        raise ValueError("direction_must_be_long_or_short")
    price = float(entry_price or 0)
    margin = float(collateral or 0)
    multiple = float(leverage or 0)
    fee_rate = max(0.0, float(fee_percent or 0)) / 100.0
    maintenance = max(0.01, min(0.95, float(maintenance_ratio or 0)))
    if price <= 0 or margin <= 0 or multiple < 1:
        raise ValueError("price_collateral_and_leverage_must_be_positive")
    notional = margin * multiple
    quantity = notional / price
    liquidation_move = (1.0 - maintenance) / multiple
    liquidation_price = price * (1.0 - liquidation_move if normalized_direction == "long" else 1.0 + liquidation_move)
    return {
        "notional": round(notional, 2),
        "quantity": round(quantity, 8),
        "open_fee": round(notional * fee_rate, 2),
        "liquidation_price": round(max(0.0001, liquidation_price), 4),
        "maintenance_equity": round(margin * maintenance, 2),
    }


def ravenhood_margin_metrics(
    direction: str,
    entry_price: Any,
    mark_price: Any,
    quantity: Any,
    collateral: Any,
    leverage: Any,
    fee_percent: Any,
    maintenance_ratio: Any,
) -> dict[str, float | bool]:
    """Mark an isolated long/short position and calculate close proceeds."""
    opening = ravenhood_margin_quote(
        direction, entry_price, collateral, leverage, fee_percent, maintenance_ratio
    )
    normalized_direction = str(direction or "").strip().lower()
    entry = float(entry_price or 0)
    mark = max(0.0001, float(mark_price or 0))
    units = max(0.0, float(quantity or 0))
    margin = max(0.0, float(collateral or 0))
    pnl = units * (mark - entry) if normalized_direction == "long" else units * (entry - mark)
    equity = margin + pnl
    close_notional = units * mark
    close_fee = max(0.0, close_notional * max(0.0, float(fee_percent or 0)) / 100.0)
    payout = max(0.0, equity - close_fee)
    maintenance_equity = float(opening["maintenance_equity"])
    return {
        **opening,
        "mark_price": round(mark, 4),
        "unrealized_pnl": round(pnl, 2),
        "equity": round(equity, 2),
        "return_percent": round((pnl / margin * 100.0) if margin else 0.0, 4),
        "close_notional": round(close_notional, 2),
        "close_fee": round(close_fee, 2),
        "estimated_payout": round(payout, 2),
        "liquidatable": equity <= maintenance_equity + 0.0001,
    }


def ravenhood_liquidation_hunt_quote(
    direction: str,
    mark_price: Any,
    liquidation_price: Any,
    intensity: str,
    max_move_percent: Any,
) -> dict[str, float | str | bool]:
    """Move one quote toward, but never through, a liquidation boundary.

    The market engine chooses the eligible position. This helper only applies
    the configured, deterministic guardrails so Local and hosted AI providers
    receive identical treatment and cannot directly force a liquidation.
    """
    normalized_direction = str(direction or "").strip().lower()
    if normalized_direction not in {"long", "short"}:
        raise ValueError("direction_must_be_long_or_short")
    mark = float(mark_price or 0)
    boundary = float(liquidation_price or 0)
    if mark <= 0 or boundary <= 0:
        raise ValueError("mark_and_liquidation_prices_must_be_positive")
    normalized_intensity = str(intensity or "light").strip().lower()
    intensity_factors = {"light": 0.20, "aggressive": 0.45, "extreme": 0.75}
    if normalized_intensity not in intensity_factors:
        raise ValueError("intensity_must_be_light_aggressive_or_extreme")
    move_cap = max(0.01, min(15.0, float(max_move_percent or 0)))
    safety_buffer = max(0.0001, mark * 0.0005)

    if normalized_direction == "long":
        usable_gap = mark - boundary - safety_buffer
        gap_before = max(0.0, (mark - boundary) / mark * 100.0)
        if usable_gap <= 0:
            return {
                "moved": False, "reason": "at_liquidation_boundary", "direction": normalized_direction,
                "old_price": round(mark, 4), "new_price": round(mark, 4),
                "liquidation_price": round(boundary, 4), "movement_percent": 0.0,
                "gap_before_percent": round(gap_before, 4), "gap_after_percent": round(gap_before, 4),
            }
        desired = usable_gap * intensity_factors[normalized_intensity]
        movement = min(desired, mark * move_cap / 100.0)
        new_price = max(boundary + safety_buffer, mark - movement)
    else:
        usable_gap = boundary - mark - safety_buffer
        gap_before = max(0.0, (boundary - mark) / mark * 100.0)
        if usable_gap <= 0:
            return {
                "moved": False, "reason": "at_liquidation_boundary", "direction": normalized_direction,
                "old_price": round(mark, 4), "new_price": round(mark, 4),
                "liquidation_price": round(boundary, 4), "movement_percent": 0.0,
                "gap_before_percent": round(gap_before, 4), "gap_after_percent": round(gap_before, 4),
            }
        desired = usable_gap * intensity_factors[normalized_intensity]
        movement = min(desired, mark * move_cap / 100.0)
        new_price = min(boundary - safety_buffer, mark + movement)

    new_price = max(0.0001, round(new_price, 4))
    movement_percent = (new_price / mark - 1.0) * 100.0
    gap_after = (
        (new_price - boundary) / new_price * 100.0
        if normalized_direction == "long"
        else (boundary - new_price) / new_price * 100.0
    )
    return {
        "moved": abs(new_price - mark) >= 0.00005,
        "reason": "pressured",
        "direction": normalized_direction,
        "old_price": round(mark, 4),
        "new_price": new_price,
        "liquidation_price": round(boundary, 4),
        "movement_percent": round(movement_percent, 4),
        "gap_before_percent": round(gap_before, 4),
        "gap_after_percent": round(max(0.0, gap_after), 4),
    }
