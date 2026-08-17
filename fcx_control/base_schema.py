from __future__ import annotations

from typing import Any


# These tables are the exchange-owned compatibility surface used by the
# autonomous engine and the player APIs.  They deliberately contain no CAD
# tables and no foreign keys to a CAD resident database.
DDL = (
    """CREATE TABLE IF NOT EXISTS system_settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS market_accounts (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL UNIQUE,
        cash_balance NUMERIC(24,2) NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES fcx_ravenhood_accounts(id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS market_account_trading_restrictions (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL,
        scope TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        reason TEXT NOT NULL,
        case_reference TEXT NOT NULL DEFAULT '',
        created_by BIGINT,
        created_by_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        released_by BIGINT,
        released_by_name TEXT NOT NULL DEFAULT '',
        released_at TEXT,
        release_note TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (account_id) REFERENCES market_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY (created_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL,
        FOREIGN KEY (released_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS market_account_restrictions_active_idx
        ON market_account_trading_restrictions(account_id) WHERE status='active'""",
    """CREATE TABLE IF NOT EXISTS market_securities (
        id SERIAL PRIMARY KEY,
        ticker TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        security_type TEXT NOT NULL DEFAULT 'stock',
        sector TEXT NOT NULL DEFAULT 'General',
        description TEXT NOT NULL DEFAULT '',
        price NUMERIC(18,4) NOT NULL,
        previous_price NUMERIC(18,4) NOT NULL,
        volatility NUMERIC(10,4) NOT NULL DEFAULT 1,
        active INTEGER NOT NULL DEFAULT 1,
        lifecycle_status TEXT NOT NULL DEFAULT 'active',
        bankruptcy_chapter TEXT,
        bankruptcy_reason TEXT,
        bankruptcy_at TEXT,
        closed_by BIGINT,
        issued_shares NUMERIC(24,6) NOT NULL DEFAULT 1000000,
        index_eligible INTEGER NOT NULL DEFAULT 1,
        margin_enabled INTEGER NOT NULL DEFAULT 1,
        margin_max_leverage NUMERIC(6,2) NOT NULL DEFAULT 5,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (closed_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
    """CREATE TABLE IF NOT EXISTS market_security_halts (
        id SERIAL PRIMARY KEY,
        security_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        reason_code TEXT NOT NULL,
        reason_label TEXT NOT NULL,
        public_notice TEXT NOT NULL DEFAULT '',
        case_reference TEXT NOT NULL DEFAULT '',
        halted_by BIGINT,
        halted_by_name TEXT NOT NULL DEFAULT '',
        halted_at TEXT NOT NULL,
        resumed_by BIGINT,
        resumed_by_name TEXT NOT NULL DEFAULT '',
        resumed_at TEXT,
        resume_note TEXT NOT NULL DEFAULT '',
        automatic_resume_at TEXT,
        engine_managed INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE,
        FOREIGN KEY (halted_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL,
        FOREIGN KEY (resumed_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS market_security_halts_active_idx
        ON market_security_halts(security_id) WHERE status='active'""",
    """CREATE TABLE IF NOT EXISTS market_security_delistings (
        id SERIAL PRIMARY KEY,
        security_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        reason_code TEXT NOT NULL,
        reason_label TEXT NOT NULL,
        public_notice TEXT NOT NULL DEFAULT '',
        case_reference TEXT NOT NULL DEFAULT '',
        delisted_by BIGINT,
        delisted_by_name TEXT NOT NULL DEFAULT '',
        delisted_at TEXT NOT NULL,
        relisted_by BIGINT,
        relisted_by_name TEXT NOT NULL DEFAULT '',
        relisted_at TEXT,
        relist_note TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE,
        FOREIGN KEY (delisted_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL,
        FOREIGN KEY (relisted_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS market_security_delistings_active_idx
        ON market_security_delistings(security_id) WHERE status='active'""",
    """CREATE TABLE IF NOT EXISTS market_holdings (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL,
        security_id INTEGER NOT NULL,
        quantity NUMERIC(24,8) NOT NULL DEFAULT 0,
        reserved_quantity NUMERIC(24,8) NOT NULL DEFAULT 0,
        average_cost NUMERIC(18,4) NOT NULL DEFAULT 0,
        UNIQUE(account_id,security_id),
        FOREIGN KEY (account_id) REFERENCES market_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS market_orders (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL,
        security_id INTEGER NOT NULL,
        side TEXT NOT NULL,
        quantity NUMERIC(24,8) NOT NULL,
        unit_price NUMERIC(18,4) NOT NULL,
        gross_amount NUMERIC(24,2) NOT NULL,
        fee_amount NUMERIC(24,2) NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (account_id) REFERENCES market_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS market_orders_created_idx ON market_orders(created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS market_orders_account_created_idx ON market_orders(account_id,created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS market_order_requests (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL,
        security_id INTEGER NOT NULL,
        side TEXT NOT NULL,
        quantity NUMERIC(24,8) NOT NULL,
        submitted_price NUMERIC(18,4) NOT NULL,
        estimated_gross NUMERIC(24,2) NOT NULL,
        estimated_fee NUMERIC(24,2) NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued',
        execute_after TEXT,
        queued_at TEXT NOT NULL,
        executed_order_id INTEGER,
        executed_unit_price NUMERIC(18,4),
        executed_gross NUMERIC(24,2),
        executed_fee NUMERIC(24,2),
        executed_at TEXT,
        cancelled_at TEXT,
        failure_reason TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (account_id) REFERENCES market_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE,
        FOREIGN KEY (executed_order_id) REFERENCES market_orders(id) ON DELETE SET NULL
    )""",
    """CREATE INDEX IF NOT EXISTS market_order_requests_queue_idx
        ON market_order_requests(status,queued_at) WHERE status IN ('queued','processing')""",
    """CREATE TABLE IF NOT EXISTS market_margin_positions (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL,
        security_id INTEGER NOT NULL,
        direction TEXT NOT NULL,
        leverage NUMERIC(6,2) NOT NULL,
        collateral NUMERIC(24,2) NOT NULL,
        quantity NUMERIC(24,8) NOT NULL,
        entry_price NUMERIC(18,4) NOT NULL,
        entry_notional NUMERIC(24,2) NOT NULL,
        open_fee NUMERIC(24,2) NOT NULL DEFAULT 0,
        liquidation_price NUMERIC(18,4) NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        opened_at TEXT NOT NULL,
        close_price NUMERIC(18,4),
        close_notional NUMERIC(24,2),
        close_fee NUMERIC(24,2),
        realized_pnl NUMERIC(24,2),
        payout_amount NUMERIC(24,2),
        settlement_status TEXT NOT NULL DEFAULT '',
        cash_balance_before NUMERIC(24,2),
        cash_balance_after NUMERIC(24,2),
        settled_at TEXT,
        close_reason TEXT NOT NULL DEFAULT '',
        closed_at TEXT,
        FOREIGN KEY (account_id) REFERENCES market_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS market_margin_positions_open_idx
        ON market_margin_positions(status,security_id) WHERE status='open'""",
    """CREATE INDEX IF NOT EXISTS market_margin_positions_account_status_idx
        ON market_margin_positions(account_id,status,opened_at DESC)""",
    """CREATE TABLE IF NOT EXISTS market_margin_order_requests (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL,
        security_id INTEGER NOT NULL,
        direction TEXT NOT NULL,
        leverage NUMERIC(6,2) NOT NULL,
        collateral NUMERIC(24,2) NOT NULL,
        submitted_price NUMERIC(18,4) NOT NULL,
        estimated_notional NUMERIC(24,2) NOT NULL,
        estimated_fee NUMERIC(24,2) NOT NULL DEFAULT 0,
        estimated_liquidation_price NUMERIC(18,4) NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        execute_after TEXT,
        queued_at TEXT NOT NULL,
        executed_position_id INTEGER,
        executed_at TEXT,
        cancelled_at TEXT,
        failure_reason TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (account_id) REFERENCES market_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE,
        FOREIGN KEY (executed_position_id) REFERENCES market_margin_positions(id) ON DELETE SET NULL
    )""",
    """CREATE INDEX IF NOT EXISTS market_margin_requests_queue_idx
        ON market_margin_order_requests(status,queued_at) WHERE status IN ('queued','processing')""",
    """CREATE TABLE IF NOT EXISTS market_system_trades (
        id SERIAL PRIMARY KEY,
        security_id INTEGER NOT NULL,
        buy_volume NUMERIC(24,8) NOT NULL DEFAULT 0,
        sell_volume NUMERIC(24,8) NOT NULL DEFAULT 0,
        buy_trade_count INTEGER NOT NULL DEFAULT 0,
        sell_trade_count INTEGER NOT NULL DEFAULT 0,
        reference_price NUMERIC(18,4) NOT NULL,
        price_change_percent NUMERIC(12,4) NOT NULL DEFAULT 0,
        source TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS market_system_trades_security_time_idx
        ON market_system_trades(security_id,created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS market_price_programs (
        id SERIAL PRIMARY KEY,
        security_id INTEGER,
        event_name TEXT NOT NULL,
        percent_change NUMERIC(12,4) NOT NULL,
        duration_minutes INTEGER NOT NULL,
        start_price NUMERIC(18,4),
        target_price NUMERIC(18,4),
        starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_by BIGINT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE,
        FOREIGN KEY (created_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
    """CREATE TABLE IF NOT EXISTS market_promo_codes (
        id SERIAL PRIMARY KEY,
        campaign_name TEXT NOT NULL,
        code_hash TEXT NOT NULL UNIQUE,
        code_hint TEXT NOT NULL,
        code_plain TEXT NOT NULL DEFAULT '',
        reward_type TEXT NOT NULL,
        cash_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
        security_id INTEGER,
        share_quantity NUMERIC(18,6) NOT NULL DEFAULT 0,
        bundle_size INTEGER NOT NULL DEFAULT 0,
        max_redemptions INTEGER NOT NULL DEFAULT 1,
        redemption_count INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_by BIGINT,
        created_by_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE SET NULL,
        FOREIGN KEY (created_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
    """CREATE TABLE IF NOT EXISTS market_promo_redemptions (
        id SERIAL PRIMARY KEY,
        promo_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        community_id TEXT NOT NULL DEFAULT '',
        reward_summary TEXT NOT NULL,
        redeemed_at TEXT NOT NULL,
        UNIQUE(promo_id,account_id),
        FOREIGN KEY (promo_id) REFERENCES market_promo_codes(id) ON DELETE CASCADE,
        FOREIGN KEY (account_id) REFERENCES market_accounts(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS market_promo_redemptions_account_created_idx
        ON market_promo_redemptions(account_id,redeemed_at DESC)""",
    """CREATE TABLE IF NOT EXISTS market_price_history (
        id SERIAL PRIMARY KEY,
        security_id INTEGER NOT NULL,
        price NUMERIC(18,4) NOT NULL,
        source TEXT NOT NULL DEFAULT 'market',
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS market_price_history_security_time_idx
        ON market_price_history(security_id,recorded_at)""",
    """CREATE TABLE IF NOT EXISTS market_index_funds (
        id SERIAL PRIMARY KEY,
        fund_key TEXT NOT NULL UNIQUE,
        security_id INTEGER NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        risk_profile TEXT NOT NULL,
        target_size INTEGER NOT NULL DEFAULT 8,
        base_nav NUMERIC(18,4) NOT NULL DEFAULT 100,
        management_fee_percent NUMERIC(8,4) NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        rebalance_interval_hours INTEGER NOT NULL DEFAULT 24,
        last_rebalanced_at TEXT,
        last_valued_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS market_index_members (
        fund_id INTEGER NOT NULL,
        security_id INTEGER NOT NULL,
        weight NUMERIC(12,8) NOT NULL,
        score NUMERIC(18,6) NOT NULL DEFAULT 0,
        reference_price NUMERIC(18,4) NOT NULL,
        market_cap_at_rebalance NUMERIC(24,2) NOT NULL DEFAULT 0,
        realized_volatility NUMERIC(18,6) NOT NULL DEFAULT 0,
        rank INTEGER NOT NULL DEFAULT 0,
        added_at TEXT NOT NULL,
        PRIMARY KEY (fund_id,security_id),
        FOREIGN KEY (fund_id) REFERENCES market_index_funds(id) ON DELETE CASCADE,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS business_issuer_companies (
        id SERIAL PRIMARY KEY,
        company_number TEXT NOT NULL UNIQUE,
        security_id INTEGER UNIQUE,
        controlling_user_id BIGINT,
        created_by BIGINT,
        assigned_by BIGINT,
        control_source TEXT NOT NULL DEFAULT 'new_ipo',
        status TEXT NOT NULL DEFAULT 'draft',
        review_status TEXT NOT NULL DEFAULT 'approved',
        company_name TEXT NOT NULL,
        ticker TEXT NOT NULL UNIQUE,
        sector TEXT NOT NULL DEFAULT 'General',
        headquarters TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        target_market_cap NUMERIC(24,2) NOT NULL DEFAULT 0,
        opening_share_price NUMERIC(18,4) NOT NULL DEFAULT 0,
        authorized_shares NUMERIC(24,8) NOT NULL DEFAULT 0,
        public_float_percent NUMERIC(8,2) NOT NULL DEFAULT 25,
        founder_shares NUMERIC(24,8) NOT NULL DEFAULT 0,
        issuer_inventory NUMERIC(24,8) NOT NULL DEFAULT 0,
        paid_in_capital NUMERIC(24,2) NOT NULL DEFAULT 0,
        treasury_balance NUMERIC(24,2) NOT NULL DEFAULT 0,
        submitted_at TEXT,
        reviewed_at TEXT,
        reviewed_by BIGINT,
        review_note TEXT NOT NULL DEFAULT '',
        scheduled_at TEXT,
        activated_at TEXT,
        bankruptcy_reason TEXT NOT NULL DEFAULT '',
        bankruptcy_filed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE SET NULL,
        FOREIGN KEY (controlling_user_id) REFERENCES fcx_ravenhood_accounts(id) ON DELETE SET NULL,
        FOREIGN KEY (created_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL,
        FOREIGN KEY (assigned_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL,
        FOREIGN KEY (reviewed_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
    """CREATE TABLE IF NOT EXISTS market_fec_asset_pool (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        balance NUMERIC(24,2) NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS market_fec_asset_ledger (
        id SERIAL PRIMARY KEY,
        event_type TEXT NOT NULL,
        amount NUMERIC(24,2) NOT NULL,
        pool_delta NUMERIC(24,2) NOT NULL,
        pool_balance_after NUMERIC(24,2) NOT NULL,
        market_account_id INTEGER,
        target_user_id BIGINT,
        target_name TEXT NOT NULL DEFAULT '',
        target_account_id TEXT NOT NULL DEFAULT '',
        target_identity_id TEXT NOT NULL DEFAULT '',
        case_reference TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        allocation_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_by BIGINT,
        created_by_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (market_account_id) REFERENCES market_accounts(id) ON DELETE SET NULL,
        FOREIGN KEY (target_user_id) REFERENCES fcx_ravenhood_accounts(id) ON DELETE SET NULL,
        FOREIGN KEY (created_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
    """CREATE TABLE IF NOT EXISTS market_fec_equity_cash_resets (
        id SERIAL PRIMARY KEY,
        affected_accounts INTEGER NOT NULL DEFAULT 0,
        removed_amount NUMERIC(24,2) NOT NULL DEFAULT 0,
        reason TEXT NOT NULL,
        created_by BIGINT,
        created_by_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (created_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
    """CREATE TABLE IF NOT EXISTS market_fec_share_resets (
        id SERIAL PRIMARY KEY,
        affected_accounts INTEGER NOT NULL DEFAULT 0,
        removed_shares NUMERIC(30,8) NOT NULL DEFAULT 0,
        reason TEXT NOT NULL,
        created_by BIGINT,
        created_by_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (created_by) REFERENCES fcx_control_admin_users(id) ON DELETE SET NULL
    )""",
)


def ensure_base_schema(db: Any) -> None:
    for statement in DDL:
        db.execute(statement)
    # CAD 1 promotion tables predate the standalone FCX schema.  Databases
    # created by the migration tool retain the legacy required ``user_id``
    # column, while centralized redemption identifies the market account and
    # community.  Make that table additive-compatible without deleting any
    # migrated campaign or redemption history.
    db.execute(
        "ALTER TABLE market_promo_codes "
        "ADD COLUMN IF NOT EXISTS created_by_name TEXT NOT NULL DEFAULT ''"
    )
    db.execute(
        "ALTER TABLE market_promo_redemptions "
        "ADD COLUMN IF NOT EXISTS community_id TEXT NOT NULL DEFAULT ''"
    )
    db.execute(
        """DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='market_promo_redemptions'
                  AND column_name='user_id'
            ) THEN
                ALTER TABLE market_promo_redemptions ALTER COLUMN user_id DROP NOT NULL;
            END IF;
        END $$"""
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS market_promo_redemptions_promo_account_idx "
        "ON market_promo_redemptions(promo_id,account_id)"
    )
    # Additive compatibility migration for databases first created by the
    # original monolith.  Reserved shares stop two communities (or two
    # retries) from selling the same position while a bank credit is pending.
    db.execute(
        "ALTER TABLE market_holdings "
        "ADD COLUMN IF NOT EXISTS reserved_quantity NUMERIC(24,8) NOT NULL DEFAULT 0"
    )
    db.execute(
        "INSERT INTO market_fec_asset_pool (id,balance,updated_at) VALUES (1,0,'') "
        "ON CONFLICT (id) DO NOTHING"
    )
