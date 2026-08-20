import unittest
from pathlib import Path


class InvestigationRestrictionTests(unittest.TestCase):
    def test_main_investigation_workspace_shows_active_restrictions(self):
        ui = (Path(__file__).resolve().parents[1] / "static" / "control.js").read_text(encoding="utf-8")
        for phrase in (
            "function activeInvestigationRestrictions",
            "Active trading restrictions",
            'state.investigationRestrictions=(workspace.restrictions||[]).filter',
            "Every resident restriction currently blocking stock trading",
            'class="release-restriction"',
            "Review & release",
            "No active resident trading restrictions",
        ):
            self.assertIn(phrase, ui)


if __name__ == "__main__":
    unittest.main()
