import unittest
from pathlib import Path


class InvestigationCustodyUiTests(unittest.TestCase):
    def test_cases_open_evidence_and_seized_assets_have_dedicated_workspace(self):
        root = Path(__file__).resolve().parents[1]
        ui = (root / "static" / "control.js").read_text(encoding="utf-8")
        api = (root / "fcx_control" / "api.py").read_text(encoding="utf-8")
        for phrase in (
            '["seized", "Seized Assets"]',
            "open-flag-investigation",
            "appendInvestigationActions",
            "Create investigation",
            "Move funds to FEC custody",
            "Return to the investigated user",
            "Delete from the system permanently",
            "Reinvest across the market by market cap",
            "CUSTODY LEDGER",
        ):
            self.assertIn(phrase, ui)
        self.assertIn('payload.disposition == "reinvest"', api)
        self.assertIn("payload.amount * cap / total_cap", api)
        self.assertIn('rounding=ROUND_DOWN', api)
        self.assertIn('previous_price=price,price=:price', api)
        self.assertIn('def normalize_authorization', api)
        self.assertIn('action=f"fec.assets.{payload.disposition}"', api)
        self.assertIn('const apiError =', ui)
        self.assertNotIn('new Error(payload.detail ||', ui)


if __name__ == "__main__":
    unittest.main()
