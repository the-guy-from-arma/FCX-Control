import unittest
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("FCX_DATABASE_URL", "postgresql://test:test@localhost/fcx_test")
os.environ.setdefault("FCX_SESSION_SECRET", "test-session-secret-that-is-long-enough-for-unit-tests-only")

from fcx_control.api import _equity_pnl_ledger


class ProfitSurveillanceTests(unittest.TestCase):
    def test_equity_ledger_attributes_weighted_buy_cost_to_sell_profit(self):
        orders = [
            {"id": 1, "security_id": 7, "side": "buy", "quantity": 10, "unit_price": 100, "gross_amount": 1000, "fee_amount": 10, "occurred_at": "2026-01-01T00:00:00+00:00", "ticker": "TEST", "name": "Test"},
            {"id": 2, "security_id": 7, "side": "buy", "quantity": 10, "unit_price": 200, "gross_amount": 2000, "fee_amount": 10, "occurred_at": "2026-01-01T01:00:00+00:00", "ticker": "TEST", "name": "Test"},
            {"id": 3, "security_id": 7, "side": "sell", "quantity": 5, "unit_price": 300, "gross_amount": 1500, "fee_amount": 10, "occurred_at": "2026-01-01T02:00:00+00:00", "ticker": "TEST", "name": "Test"},
        ]
        with patch("fcx_control.api.all_rows", return_value=orders):
            ledger = _equity_pnl_ledger(object(), 22)
        self.assertEqual(ledger[0]["realized_pnl"], Decimal("0.00"))
        self.assertEqual(ledger[1]["realized_pnl"], Decimal("0.00"))
        self.assertEqual(ledger[2]["cost_basis"], Decimal("755.00"))
        self.assertEqual(ledger[2]["realized_pnl"], Decimal("735.00"))

    def test_fec_ui_exposes_configurable_flag_and_trade_evidence(self):
        root = Path(__file__).resolve().parents[1]
        api = (root / "fcx_control" / "api.py").read_text(encoding="utf-8")
        ui = (root / "static" / "control.js").read_text(encoding="utf-8")
        service = (root / "fcx_engine" / "service.py").read_text(encoding="utf-8")
        for phrase in ("profit_surveillance_gain_threshold", "profit_surveillance_window_hours", "profit_surveillance_auto_restrict", "_evaluate_profit_surveillance"):
            self.assertIn(phrase, api)
        for phrase in ("Excessive-profit trading flag", "Gain threshold", "Time window (hours)", "Cost basis", "Trade P/L", "Scan all accounts now"):
            self.assertIn(phrase, ui)
        self.assertIn('id="fec_profit_surveillance"', service)


if __name__ == "__main__":
    unittest.main()
