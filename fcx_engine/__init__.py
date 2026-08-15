"""Autonomous FCX market-engine compatibility layer.

The engine owns only ``fcx_engine_*`` state.  Existing Ravenhood resident
accounts, holdings, orders, and Arma Bank Bridge records remain authoritative.
"""

from .engine import (
    apply_dividend,
    apply_stock_split,
    admin_snapshot,
    ensure_schema,
    index_constituent_counts,
    run_due_cycles,
    run_manual_cycle,
    seed_investors,
)
from .sandbox import run_sandbox

__all__ = [
    "apply_dividend",
    "apply_stock_split",
    "admin_snapshot",
    "ensure_schema",
    "index_constituent_counts",
    "run_due_cycles",
    "run_manual_cycle",
    "run_sandbox",
    "seed_investors",
]
