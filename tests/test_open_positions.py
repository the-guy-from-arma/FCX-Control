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

    def test_fec_can_liquidate_only_leveraged_rows(self):
        root = Path(__file__).resolve().parents[1]
        api = (root / "fcx_control" / "api.py").read_text(encoding="utf-8")
        ui = (root / "static" / "control.js").read_text(encoding="utf-8")
        self.assertIn('@router.post("/admin/live-positions/{position_id}/liquidate")', api)
        self.assertIn('close_reason=\'fec_liquidation\'', api)
        self.assertIn('action="fec.margin_position.liquidated"', api)
        self.assertIn('class="position-liquidate danger-action"', ui)
        self.assertIn('row.position_type==="leverage"', ui)
        self.assertIn('/liquidate`,"POST"', ui)


if __name__ == "__main__":
    unittest.main()
