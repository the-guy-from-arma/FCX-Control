import unittest
from pathlib import Path


class OverviewLeverageControlTests(unittest.TestCase):
    def test_overview_can_release_community_longs_and_shorts(self):
        source = (Path(__file__).parents[1] / "static" / "control.js").read_text(encoding="utf-8")
        self.assertIn('const communityLeverage = yes(settings.market_margin_enabled ?? "1")', source)
        self.assertIn('data-field="margin_enabled"', source)
        self.assertIn('Community Longs & Shorts', source)
        self.assertIn('communityLeverage?"Restrict":"Release"', source)


if __name__ == "__main__":
    unittest.main()
