from __future__ import annotations

from datetime import datetime, timezone

from .config import settings
from .db import execute, one, transaction
from .security import hash_password, protected_hash


DDL = (
    """CREATE TABLE IF NOT EXISTS fcx_control_admin_users (
        id BIGSERIAL PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        roles_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        source_cad TEXT NOT NULL DEFAULT '',
        source_user_id TEXT NOT NULL DEFAULT '',
        last_login_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fcx_control_admin_sessions (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        csrf_hash TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ,
        ip_address TEXT NOT NULL DEFAULT '',
        user_agent TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS fcx_control_sessions_user_idx
        ON fcx_control_admin_sessions(user_id,expires_at)""",
    """CREATE TABLE IF NOT EXISTS fcx_communities (
        id BIGSERIAL PRIMARY KEY,
        community_id TEXT NOT NULL UNIQUE,
        community_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        connection_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        trading_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        buy_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        sell_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        account_creation_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        scheduled_open TIMESTAMPTZ,
        scheduled_close TIMESTAMPTZ,
        suspended BOOLEAN NOT NULL DEFAULT FALSE,
        suspension_reason TEXT NOT NULL DEFAULT '',
        bank_bridge_url TEXT NOT NULL DEFAULT '',
        bank_secret_env TEXT NOT NULL DEFAULT '',
        last_seen_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fcx_community_credentials (
        id BIGSERIAL PRIMARY KEY,
        community_id TEXT NOT NULL,
        credential_id TEXT NOT NULL UNIQUE,
        secret_hash TEXT NOT NULL,
        scopes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        last_used_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        created_by BIGINT,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS fcx_community_credentials_lookup_idx
        ON fcx_community_credentials(credential_id,active)""",
    """CREATE TABLE IF NOT EXISTS fcx_ravenhood_accounts (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        restriction_reason TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fcx_ravenhood_links (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL,
        community_id TEXT NOT NULL,
        community_user_id TEXT NOT NULL,
        bohemia_identity_id TEXT NOT NULL DEFAULT '',
        verified BOOLEAN NOT NULL DEFAULT FALSE,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE(community_id,community_user_id),
        UNIQUE(account_id,community_id)
    )""",
    """CREATE INDEX IF NOT EXISTS fcx_ravenhood_links_identity_idx
        ON fcx_ravenhood_links(bohemia_identity_id)""",
    """CREATE TABLE IF NOT EXISTS fcx_settlements (
        id BIGSERIAL PRIMARY KEY,
        settlement_id TEXT NOT NULL UNIQUE,
        idempotency_key TEXT NOT NULL,
        community_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        community_user_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        amount NUMERIC(24,2) NOT NULL,
        currency TEXT NOT NULL DEFAULT 'FC',
        state TEXT NOT NULL DEFAULT 'CREATED',
        bank_reference TEXT NOT NULL DEFAULT '',
        order_reference TEXT NOT NULL DEFAULT '',
        failure_code TEXT NOT NULL DEFAULT '',
        failure_message TEXT NOT NULL DEFAULT '',
        request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE(community_id,idempotency_key)
    )""",
    """CREATE INDEX IF NOT EXISTS fcx_settlements_state_idx
        ON fcx_settlements(community_id,state,created_at)""",
    """ALTER TABLE fcx_settlements ADD COLUMN IF NOT EXISTS wallet_reserved_at TIMESTAMPTZ""",
    """ALTER TABLE fcx_settlements ADD COLUMN IF NOT EXISTS wallet_applied_at TIMESTAMPTZ""",
    """ALTER TABLE fcx_settlements ADD COLUMN IF NOT EXISTS wallet_reversed_at TIMESTAMPTZ""",
    """ALTER TABLE fcx_settlements ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ""",
    """ALTER TABLE fcx_settlements ADD COLUMN IF NOT EXISTS cancel_reason TEXT NOT NULL DEFAULT ''""",
    """CREATE TABLE IF NOT EXISTS fcx_trade_requests (
        id BIGSERIAL PRIMARY KEY,
        trade_request_id TEXT NOT NULL UNIQUE,
        idempotency_key TEXT NOT NULL,
        community_id TEXT NOT NULL,
        community_user_id TEXT NOT NULL,
        ravenhood_account_id TEXT NOT NULL,
        market_account_id BIGINT NOT NULL,
        security_id BIGINT NOT NULL,
        side TEXT NOT NULL,
        quantity NUMERIC(24,8) NOT NULL,
        submitted_price NUMERIC(18,4) NOT NULL,
        estimated_gross NUMERIC(24,2) NOT NULL,
        estimated_fee NUMERIC(24,2) NOT NULL,
        estimated_total NUMERIC(24,2) NOT NULL,
        status TEXT NOT NULL DEFAULT 'CREATED',
        debit_settlement_id TEXT NOT NULL DEFAULT '',
        credit_settlement_id TEXT NOT NULL DEFAULT '',
        executed_order_id BIGINT,
        executed_price NUMERIC(18,4),
        executed_gross NUMERIC(24,2),
        executed_fee NUMERIC(24,2),
        failure_code TEXT NOT NULL DEFAULT '',
        failure_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        executed_at TIMESTAMPTZ,
        settled_at TIMESTAMPTZ,
        UNIQUE(community_id,idempotency_key),
        FOREIGN KEY (market_account_id) REFERENCES market_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY (security_id) REFERENCES market_securities(id) ON DELETE RESTRICT,
        FOREIGN KEY (executed_order_id) REFERENCES market_orders(id) ON DELETE SET NULL
    )""",
    """CREATE INDEX IF NOT EXISTS fcx_trade_requests_account_idx
        ON fcx_trade_requests(ravenhood_account_id,created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS fcx_trade_requests_status_idx
        ON fcx_trade_requests(status,updated_at)""",
    """CREATE TABLE IF NOT EXISTS fcx_investigations (
        id BIGSERIAL PRIMARY KEY,
        case_id TEXT NOT NULL UNIQUE,
        account_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        priority TEXT NOT NULL DEFAULT 'normal',
        summary TEXT NOT NULL,
        notes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        opened_by BIGINT NOT NULL,
        assigned_to BIGINT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fcx_audit_log (
        id BIGSERIAL PRIMARY KEY,
        actor_type TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        actor_role TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        target_type TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        previous_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        new_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        reason TEXT NOT NULL DEFAULT '',
        ip_address TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS fcx_audit_log_created_idx ON fcx_audit_log(created_at DESC)""",
)


def _requires_exchange_schema(statement: str) -> bool:
    normalized = " ".join(statement.split()).lower()
    return normalized.startswith("create table if not exists fcx_trade_requests") or normalized.startswith(
        "create index if not exists fcx_trade_requests_"
    )


def _ensure_bootstrap_admin(connection, now: datetime) -> None:
    admin = one(connection, "SELECT id FROM fcx_control_admin_users LIMIT 1")
    if settings.bootstrap_email and settings.bootstrap_password and (admin is None or settings.bootstrap_force_sync):
        execute(
            connection,
            """INSERT INTO fcx_control_admin_users
                (email,display_name,password_hash,roles_json,active,created_at,updated_at)
                VALUES (:email,:name,:password,CAST(:roles AS jsonb),TRUE,:now,:now)
                ON CONFLICT(email) DO UPDATE SET
                    display_name=excluded.display_name,
                    password_hash=excluded.password_hash,
                    roles_json=excluded.roles_json,
                    active=TRUE,
                    updated_at=excluded.updated_at""",
            {
                "email": settings.bootstrap_email,
                "name": "FCX Bootstrap Administrator",
                "password": hash_password(settings.bootstrap_password),
                "roles": '["super_admin","developer","commissioner","fec_admin","fcx_admin"]',
                "now": now,
            },
        )


def _ensure_community_credential(
    connection,
    now: datetime,
    community_id: str,
    community_name: str,
    presented: str,
    variable_prefix: str,
    bank_bridge_url: str = "",
    bank_secret_env: str = "",
) -> None:
    if not community_id and not presented:
        return
    if not community_id or not presented:
        raise RuntimeError(
            f"{variable_prefix}_COMMUNITY_ID and {variable_prefix}_COMMUNITY_API_KEY must be configured together"
        )
    if "." not in presented:
        raise RuntimeError(f"{variable_prefix}_COMMUNITY_API_KEY must use the credential_id.secret format")
    credential_id, secret = presented.split(".", 1)
    if not credential_id.startswith("fcx_") or not secret:
        raise RuntimeError(f"{variable_prefix}_COMMUNITY_API_KEY is malformed")
    if bool(bank_bridge_url) != bool(bank_secret_env):
        raise RuntimeError(
            f"{variable_prefix}_BANK_BRIDGE_URL and {variable_prefix}_BANK_SECRET_ENV must be configured together"
        )

    execute(
        connection,
        """INSERT INTO fcx_communities
            (community_id,community_name,status,connection_enabled,trading_enabled,
             buy_enabled,sell_enabled,account_creation_enabled,suspended,
             created_at,updated_at)
            VALUES (:community_id,:community_name,'active',TRUE,TRUE,TRUE,TRUE,TRUE,FALSE,:now,:now)
            ON CONFLICT(community_id) DO NOTHING""",
        {
            "community_id": community_id,
            "community_name": community_name or community_id,
            "now": now,
        },
    )

    # Bridge routing belongs to the FCX community record. Only overwrite it
    # when both bootstrap variables are explicitly present so an ordinary
    # restart cannot erase an operator-managed route.
    if bank_bridge_url and bank_secret_env:
        execute(
            connection,
            """UPDATE fcx_communities
                SET bank_bridge_url=:bank_bridge_url,
                    bank_secret_env=:bank_secret_env,
                    updated_at=:now
                WHERE community_id=:community_id""",
            {
                "community_id": community_id,
                "bank_bridge_url": bank_bridge_url.rstrip("/"),
                "bank_secret_env": bank_secret_env,
                "now": now,
            },
        )

    execute(
        connection,
        """INSERT INTO fcx_community_credentials
            (community_id,credential_id,secret_hash,scopes_json,active,revoked_at,created_at)
            VALUES (:community_id,:credential_id,:secret_hash,
                    '[\"account:link\",\"market:read\",\"settlement:write\",\"trade:write\"]'::jsonb,
                    TRUE,NULL,:now)
            ON CONFLICT(credential_id) DO UPDATE SET
                community_id=excluded.community_id,
                secret_hash=excluded.secret_hash,
                scopes_json=excluded.scopes_json,
                active=TRUE,
                revoked_at=NULL""",
        {
            "community_id": community_id,
            "credential_id": credential_id,
            "secret_hash": protected_hash(secret),
            "now": now,
        },
    )


def _ensure_bootstrap_community_credentials(connection, now: datetime) -> None:
    _ensure_community_credential(
        connection,
        now,
        settings.bootstrap_community_id,
        settings.bootstrap_community_name,
        settings.bootstrap_community_api_key,
        "FCX_BOOTSTRAP",
        settings.bootstrap_community_bank_bridge_url,
        settings.bootstrap_community_bank_secret_env,
    )
    _ensure_community_credential(
        connection,
        now,
        settings.bootstrap_cad2_community_id,
        settings.bootstrap_cad2_community_name,
        settings.bootstrap_cad2_community_api_key,
        "FCX_BOOTSTRAP_CAD2",
        settings.bootstrap_cad2_community_bank_bridge_url,
        settings.bootstrap_cad2_community_bank_secret_env,
    )


def ensure_identity_schema() -> None:
    """Create FCX control identities before exchange tables reference them."""
    now = datetime.now(timezone.utc)
    with transaction() as connection:
        for statement in DDL:
            if not _requires_exchange_schema(statement):
                execute(connection, statement)
        _ensure_bootstrap_admin(connection, now)
        _ensure_bootstrap_community_credentials(connection, now)


def ensure_schema() -> None:
    now = datetime.now(timezone.utc)
    with transaction() as connection:
        for statement in DDL:
            execute(connection, statement)
        _ensure_bootstrap_admin(connection, now)
        _ensure_bootstrap_community_credentials(connection, now)
