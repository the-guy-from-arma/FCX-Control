import ast
import unittest
from pathlib import Path


class TradeRequestModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).parents[1] / "fcx_control" / "api.py").read_text(encoding="utf-8"))
        cls.fields = {
            node.name: {item.target.id for item in node.body if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)}
            for node in tree.body if isinstance(node, ast.ClassDef)
        }

    def test_margin_order_does_not_require_stock_order_fields(self):
        self.assertEqual(self.fields["MarginOrderRequest"], {"community_user_id", "account_id", "ticker", "direction", "collateral", "leverage"})

    def test_stock_order_keeps_side_and_quantity(self):
        self.assertEqual(self.fields["TradeOrderRequest"], {"idempotency_key", "community_user_id", "account_id", "ticker", "side", "quantity"})


if __name__ == "__main__":
    unittest.main()
