import unittest
from pathlib import Path


class AlertReviewTests(unittest.TestCase):
    def test_overview_alerts_open_evidence_with_advised_thresholds(self):
        root = Path(__file__).resolve().parents[1]
        api = (root / "fcx_control" / "api.py").read_text(encoding="utf-8")
        ui = (root / "static" / "control.js").read_text(encoding="utf-8")
        config = (root / "fcx_engine" / "config.py").read_text(encoding="utf-8")
        engine = (root / "fcx_engine" / "engine.py").read_text(encoding="utf-8")
        service = (root / "fcx_engine" / "service.py").read_text(encoding="utf-8")
        for phrase in ("_risk_flag_recommendation", '/admin/risk-flags/{flag_id}', "observed + buffer", "engine.risk_flag.reviewed"):
            self.assertIn(phrase, api)
        for phrase in ("risk-alert-review", "Advised adjustment", "Apply advised adjustment and resolve", "Dismiss without adjustment"):
            self.assertIn(phrase, ui)
        self.assertIn("company_distress_threshold", config)
        self.assertIn("risk >= config.company_distress_threshold", engine)
        self.assertIn("company_distress_threshold", service)


if __name__ == "__main__":
    unittest.main()
