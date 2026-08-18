import unittest
from pathlib import Path


class OpenPositionFeedTests(unittest.TestCase):
    def test_live_positions_include_stock_holdings_and_leverage(self):
        root = Path(__file__).resolve().parents[1]
        api = (root / "fcx_control" / "api.py").read_text(encoding="utf-8")
        ui = (root / "static" / "control.js").read_text(encoding="utf-8")
        for phrase in ("position_type: Literal", "'equity' AS position_type", "'leverage' AS position_type", "market_holdings h", "h.average_cost AS entry_price"):
            self.assertIn(phrase, api)
        for phrase in ("Stock holdings", "Stock holdings only", "Stocks and leverage", "No open stock or leverage positions"):
            self.assertIn(phrase, ui)
        self.assertIn('row.position_type==="leverage"', ui)


if __name__ == "__main__":
    unittest.main()
