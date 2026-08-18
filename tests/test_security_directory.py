import unittest
from pathlib import Path


class SecurityDirectoryTests(unittest.TestCase):
    def test_operations_keeps_halted_and_delisted_securities_visible(self):
        root = Path(__file__).resolve().parents[1]
        api = (root / "fcx_control" / "api.py").read_text(encoding="utf-8")
        ui = (root / "static" / "control.js").read_text(encoding="utf-8")
        for phrase in ("AS exchange_status", "active_halt_id", "active_delisting_id", "LEFT JOIN LATERAL"):
            self.assertIn(phrase, api)
        for phrase in ("Active, halted, and delisted securities", "security-directory-search", "security-directory-status", "data-security-directory-row", "No securities match this filter"):
            self.assertIn(phrase, ui)
        self.assertIn('tradable = securities.filter(row => row.exchange_status === "active")', ui)


if __name__ == "__main__":
    unittest.main()
