from decimal import Decimal
import unittest

from fcx_control.indexes import market_cap_weights, rank_by_market_cap, security_market_cap


class IndexMarketCapTests(unittest.TestCase):
    def test_market_cap_uses_live_price_and_issued_shares(self):
        self.assertEqual(
            security_market_cap({"price": "12.50", "issued_shares": "2000"}),
            Decimal("25000.00"),
        )

    def test_ranking_selects_largest_current_companies(self):
        rows = [
            {"ticker": "SMALL", "price": 5, "issued_shares": 100},
            {"ticker": "LARGE", "price": 20, "issued_shares": 1000},
            {"ticker": "MID", "price": 10, "issued_shares": 500},
        ]
        self.assertEqual([row["ticker"] for row in rank_by_market_cap(rows, 2)], ["LARGE", "MID"])

    def test_weights_sum_exactly_to_one(self):
        rows = [
            {"ticker": "A", "price": 10, "issued_shares": 100},
            {"ticker": "B", "price": 20, "issued_shares": 100},
            {"ticker": "C", "price": 30, "issued_shares": 100},
        ]
        weights = market_cap_weights(rows)
        self.assertEqual(sum(weights), Decimal("1"))
        self.assertGreater(weights[2], weights[1])
        self.assertGreater(weights[1], weights[0])

    def test_zero_cap_basket_is_equal_weighted(self):
        weights = market_cap_weights([
            {"ticker": "A", "price": 0, "issued_shares": 0},
            {"ticker": "B", "price": 0, "issued_shares": 0},
        ])
        self.assertEqual(weights, [Decimal("0.50000000"), Decimal("0.50000000")])


if __name__ == "__main__":
    unittest.main()
