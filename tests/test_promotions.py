import hashlib
import unittest

from fcx_control.base_schema import ensure_base_schema
from fcx_control.promotions import promotion_code_hashes


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


class PromotionCompatibilityTests(unittest.TestCase):
    def test_migrated_hyphenated_codes_keep_legacy_hash_compatibility(self):
        canonical, legacy = promotion_code_hashes(" Relaunch-2026 ")
        self.assertEqual(canonical, hashlib.sha256(b"RELAUNCH2026").hexdigest())
        self.assertEqual(legacy, hashlib.sha256(b"RELAUNCH-2026").hexdigest())

    def test_unpunctuated_codes_have_the_same_hash(self):
        canonical, legacy = promotion_code_hashes("relaunch2026")
        self.assertEqual(canonical, legacy)

    def test_startup_repairs_legacy_promotion_redemption_schema(self):
        connection = RecordingConnection()
        ensure_base_schema(connection)
        sql = "\n".join(connection.statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS community_id", sql)
        self.assertIn("ALTER COLUMN user_id DROP NOT NULL", sql)
        self.assertIn("market_promo_redemptions_promo_account_idx", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS created_by_name", sql)

    def test_startup_repairs_legacy_fec_custody_schema(self):
        connection = RecordingConnection()
        ensure_base_schema(connection)
        sql = "\n".join(connection.statements)
        self.assertIn("ALTER TABLE market_fec_asset_ledger ADD COLUMN IF NOT EXISTS case_reference", sql)
        self.assertIn("ALTER TABLE market_fec_asset_ledger ADD COLUMN IF NOT EXISTS allocation_json", sql)
        self.assertIn("ALTER TABLE market_fec_asset_ledger ADD COLUMN IF NOT EXISTS target_identity_id", sql)
        self.assertIn("ALTER TABLE market_fec_asset_pool ADD COLUMN IF NOT EXISTS updated_at", sql)


if __name__ == "__main__":
    unittest.main()
