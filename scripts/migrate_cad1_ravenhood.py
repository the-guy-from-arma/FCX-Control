#!/usr/bin/env python3
"""Resumable, non-destructive CAD 1 -> FCX/Ravenhood data migration.

The source CAD database is strictly read-only.  Every FCX-owned source table is
first copied into an isolated staging schema in the FCX database.  The staged
rows are then promoted into the operational FCX tables while CAD-local user
identifiers are mapped to standalone Ravenhood identities.

Runtime CAD services must never receive ``FCX_DATABASE_URL``.  This utility is
the only component that needs both database URLs, and the source URL should be
removed from Railway immediately after the migration is verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator, Sequence

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


STAGE_SCHEMA = "fcx_cad1_stage"
COMMUNITY_ID = os.environ.get("SOURCE_CAD1_COMMUNITY_ID", "faircroft").strip() or "faircroft"

EXACT_TABLES = {
    "system_settings",
    "business_legacy_archive",
}
TABLE_PREFIXES = (
    "market_",
    "business_issuer_",
    "fcx_engine_",
)

TARGET_REQUIRED_TABLES = (
    "fcx_ravenhood_accounts",
    "fcx_ravenhood_links",
    "fcx_control_admin_users",
    "system_settings",
)

# Dependency order matters because the operational FCX schema deliberately
# preserves relational constraints without any dependency on CAD tables.
TABLE_PRIORITY = {
    "market_securities": 10,
    "market_accounts": 20,
    "market_holdings": 30,
    "market_orders": 31,
    "market_order_requests": 32,
    "market_margin_positions": 33,
    "market_margin_order_requests": 34,
    "market_security_halts": 35,
    "market_security_delistings": 36,
    "market_system_trades": 37,
    "market_price_programs": 38,
    "market_price_history": 39,
    "market_index_funds": 40,
    "market_index_members": 41,
    "business_issuer_companies": 50,
    "business_issuer_ipo_reviews": 51,
    "business_issuer_funding_batches": 52,
    "business_issuer_funding_chunks": 53,
    "business_issuer_ledger": 54,
    "business_issuer_market_cap_history": 55,
    "business_issuer_announcements": 56,
    "business_issuer_assignments": 57,
    "business_legacy_archive": 58,
}

PLAYER_USER_COLUMNS: dict[str, tuple[str, ...]] = {
    "market_accounts": ("user_id",),
    "market_fec_asset_ledger": ("target_user_id",),
    "market_cash_codes": ("target_user_id", "used_by"),
    "market_cash_transactions": ("processed_by",),
    "market_promo_redemptions": ("user_id",),
    "business_issuer_companies": ("controlling_user_id",),
    "business_issuer_ipo_reviews": ("applicant_user_id",),
    "business_issuer_funding_batches": ("user_id",),
    "business_issuer_ledger": ("user_id",),
    "business_issuer_announcements": ("created_by",),
    "business_issuer_assignments": ("user_id",),
    # Engine surveillance flags identify the resident Ravenhood account under
    # review, not an FCX administrator.  Remap the legacy CAD user id to the
    # standalone account identity just like market_accounts.user_id.
    "fcx_engine_risk_flags": ("user_id",),
}

ADMIN_USER_COLUMNS: dict[str, tuple[str, ...]] = {
    "market_account_trading_restrictions": ("created_by", "released_by"),
    "market_security_halts": ("halted_by", "resumed_by"),
    "market_security_delistings": ("delisted_by", "relisted_by"),
    "market_securities": ("closed_by",),
    "market_price_programs": ("created_by",),
    "market_events": ("created_by",),
    "market_liquidation_hunt_events": ("created_by",),
    "market_fec_asset_ledger": ("created_by",),
    "market_fec_equity_cash_resets": ("created_by",),
    "market_fec_share_resets": ("created_by",),
    "market_security_writeoffs": ("closed_by",),
    "market_cash_codes": ("created_by",),
    "market_promo_codes": ("created_by",),
    "business_issuer_companies": ("created_by", "assigned_by", "reviewed_by"),
    "business_issuer_ipo_reviews": ("reviewed_by",),
    "business_issuer_assignments": ("actor_id",),
    # These are historical control-plane actors.  Preserve their audit trail
    # as disabled imported FCX actors; never grant a migrated CAD credential
    # access to the standalone control service.
    "fcx_engine_corporate_actions": ("created_by",),
    "fcx_engine_deployments": ("deployed_by",),
}

MONEY_CHECKS: dict[str, tuple[str, ...]] = {
    "market_accounts": ("cash_balance",),
    "market_holdings": ("quantity", "reserved_quantity", "average_cost"),
    "market_orders": ("quantity", "gross_amount", "fee_amount"),
    "market_order_requests": ("quantity", "estimated_gross", "estimated_fee", "executed_gross", "executed_fee"),
    "market_margin_positions": ("collateral", "entry_notional", "realized_pnl", "payout_amount"),
    "market_margin_order_requests": ("collateral", "estimated_notional", "estimated_fee"),
    "market_cash_transactions": ("amount",),
    "market_transfers": ("quantity", "fee_amount"),
    "market_securities": ("price", "issued_shares"),
    "business_issuer_companies": ("target_market_cap", "paid_in_capital", "treasury_balance"),
    "business_issuer_ledger": ("amount",),
    "business_issuer_market_cap_history": ("amount", "market_cap_before", "market_cap_after"),
}

MIGRATION_DDL = (
    """CREATE TABLE IF NOT EXISTS fcx_migration_runs (
        id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL UNIQUE,
        source_name TEXT NOT NULL,
        community_id TEXT NOT NULL,
        phase TEXT NOT NULL,
        status TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL DEFAULT '',
        table_count INTEGER NOT NULL DEFAULT 0,
        row_count BIGINT NOT NULL DEFAULT 0,
        reconciliation_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        started_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ,
        failure_message TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS fcx_migration_table_results (
        id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        source_rows BIGINT NOT NULL DEFAULT 0,
        staged_rows BIGINT NOT NULL DEFAULT 0,
        promoted_rows BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE(run_id,table_name)
    )""",
    """CREATE TABLE IF NOT EXISTS fcx_migration_user_map (
        id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL,
        source_user_id BIGINT NOT NULL,
        source_display_name TEXT NOT NULL DEFAULT '',
        source_email TEXT NOT NULL DEFAULT '',
        bohemia_identity_id TEXT NOT NULL DEFAULT '',
        ravenhood_account_id TEXT,
        ravenhood_internal_id BIGINT,
        admin_internal_id BIGINT,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE(run_id,source_user_id),
        UNIQUE(run_id,ravenhood_account_id)
    )""",
)

# Minimal standalone identity/control surface required before legacy exchange
# rows can be promoted.  The FCX service will subsequently apply its complete
# schema on startup.  Keeping this bootstrap here allows a one-time Railway
# migration job to initialize a brand-new FCX database without ever giving a
# CAD runtime the target database credential.
TARGET_BOOTSTRAP_DDL = (
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
    """CREATE TABLE IF NOT EXISTS system_settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_url(value: str) -> str:
    value = value.strip()
    if value.startswith("postgresql+psycopg://"):
        return "postgresql://" + value[len("postgresql+psycopg://"):]
    if value.startswith("postgres://"):
        return "postgresql://" + value[len("postgres://"):]
    return value


def required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return normalize_url(value)


def table_allowed(name: str) -> bool:
    return name in EXACT_TABLES or name.startswith(TABLE_PREFIXES)


def safe_ident(name: str) -> sql.Identifier:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return sql.Identifier(name)


def fetch_tables(connection: psycopg.Connection[Any]) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='public' AND table_type='BASE TABLE'
               ORDER BY table_name"""
        )
        return [str(row[0]) for row in cursor.fetchall() if table_allowed(str(row[0]))]


def columns(connection: psycopg.Connection[Any], schema: str, table: str) -> list[dict[str, Any]]:
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT a.attname AS name,pg_catalog.format_type(a.atttypid,a.atttypmod) AS data_type,
                      a.attnotnull AS not_null,
                      pg_get_expr(d.adbin,d.adrelid) AS column_default,
                      a.attidentity AS identity_kind
               FROM pg_catalog.pg_attribute a
               JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
               JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
               LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
               WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped
               ORDER BY a.attnum""",
            (schema, table),
        )
        return list(cursor.fetchall())


def primary_key_columns(connection: psycopg.Connection[Any], schema: str, table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT a.attname
               FROM pg_index i
               JOIN pg_class c ON c.oid=i.indrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace
               JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum,ord) ON TRUE
               JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum
               WHERE n.nspname=%s AND c.relname=%s AND i.indisprimary
               ORDER BY k.ord""",
            (schema, table),
        )
        return [str(row[0]) for row in cursor.fetchall()]


def source_foreign_keys(
    connection: psycopg.Connection[Any], tables: Sequence[str]
) -> list[dict[str, Any]]:
    """Return ordered source foreign-key columns for the migration surface."""
    if not tables:
        return []
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT con.conname AS constraint_name,
                      child.relname AS child_table,
                      parent.relname AS parent_table,
                      array_agg(child_col.attname ORDER BY key_cols.ordinality) AS child_columns,
                      array_agg(parent_col.attname ORDER BY key_cols.ordinality) AS parent_columns
               FROM pg_constraint con
               JOIN pg_class child ON child.oid=con.conrelid
               JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace
               JOIN pg_class parent ON parent.oid=con.confrelid
               JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
               JOIN unnest(con.conkey,con.confkey) WITH ORDINALITY
                    AS key_cols(child_attnum,parent_attnum,ordinality) ON TRUE
               JOIN pg_attribute child_col
                    ON child_col.attrelid=child.oid AND child_col.attnum=key_cols.child_attnum
               JOIN pg_attribute parent_col
                    ON parent_col.attrelid=parent.oid AND parent_col.attnum=key_cols.parent_attnum
               WHERE con.contype='f'
                 AND child_ns.nspname='public'
                 AND parent_ns.nspname='public'
                 AND child.relname=ANY(%s)
               GROUP BY con.conname,child.relname,parent.relname
               ORDER BY child.relname,con.conname""",
            (list(tables),),
        )
        return [dict(row) for row in cursor.fetchall()]


def validate_user_reference_registry(
    source: psycopg.Connection[Any], tables: Sequence[str]
) -> list[dict[str, str]]:
    """Refuse to migrate a CAD user FK unless its FCX identity meaning is declared."""
    declared = {
        (table, column): "resident"
        for table, names in PLAYER_USER_COLUMNS.items()
        for column in names
    }
    declared.update(
        {
            (table, column): "audit_actor"
            for table, names in ADMIN_USER_COLUMNS.items()
            for column in names
        }
    )
    references: list[dict[str, str]] = []
    missing: list[str] = []
    for foreign_key in source_foreign_keys(source, tables):
        if foreign_key["parent_table"] != "users":
            continue
        for column in foreign_key["child_columns"]:
            key = (str(foreign_key["child_table"]), str(column))
            role = declared.get(key)
            references.append({"table": key[0], "column": key[1], "role": role or "unmapped"})
            if role is None:
                missing.append(f"{key[0]}.{key[1]}")
    if missing:
        raise RuntimeError(
            "Migration refused: CAD user references need an explicit resident/audit mapping: "
            + ", ".join(sorted(missing))
        )
    return references


def table_exists(connection: psycopg.Connection[Any], schema: str, table: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", (f'{schema}."{table}"',))
        return cursor.fetchone()[0] is not None


def database_identity(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    """Return enough server identity to detect aliases for the same database."""
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT current_database() AS database_name,
                      COALESCE(inet_server_addr()::text,'local') AS server_address,
                      COALESCE(inet_server_port(),0) AS server_port,
                      current_user AS database_user,
                      current_setting('transaction_read_only') AS transaction_read_only"""
        )
        return dict(cursor.fetchone())


def target_preflight(
    source: psycopg.Connection[Any], target: psycopg.Connection[Any], tables: Sequence[str]
) -> dict[str, Any]:
    source_identity = database_identity(source)
    target_identity = database_identity(target)
    same_database = (
        source_identity["database_name"] == target_identity["database_name"]
        and source_identity["server_address"] == target_identity["server_address"]
        and source_identity["server_port"] == target_identity["server_port"]
    )
    missing_required = [
        table for table in TARGET_REQUIRED_TABLES if not table_exists(target, "public", table)
    ]
    target_rows: dict[str, int] = {}
    for table in tables:
        if table == "system_settings" or not table_exists(target, "public", table):
            continue
        target_rows[table] = row_count(target, "public", table)
    nonempty_operational_tables = {
        table: count for table, count in target_rows.items() if count > 0
    }
    return {
        "same_database": same_database,
        "source": source_identity,
        "target": target_identity,
        "missing_required_target_tables": missing_required,
        "target_existing_rows": target_rows,
        "target_nonempty_operational_tables": nonempty_operational_tables,
        "ready": not same_database and not missing_required,
    }


def row_count(connection: psycopg.Connection[Any], schema: str, table: str, *, settings_only: bool = False) -> int:
    query = sql.SQL("SELECT COUNT(*) FROM {}.{}").format(safe_ident(schema), safe_ident(table))
    if settings_only:
        query += sql.SQL(" WHERE setting_key LIKE 'market_%' OR setting_key LIKE 'fcx_%' OR setting_key LIKE 'business_ipo_%' OR setting_key LIKE 'business_issuer_%'")
    with connection.cursor() as cursor:
        cursor.execute(query)
        return int(cursor.fetchone()[0])


def ensure_migration_schema(target: psycopg.Connection[Any]) -> None:
    with target.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(safe_ident(STAGE_SCHEMA)))
        for statement in MIGRATION_DDL:
            cursor.execute(statement)
        # Earlier development runs created these columns as NOT NULL.  An
        # audit-only CAD actor is intentionally not a Ravenhood account, so
        # allow the mapping row to contain only its FCX admin placeholder.
        cursor.execute("ALTER TABLE fcx_migration_user_map ALTER COLUMN ravenhood_account_id DROP NOT NULL")
        cursor.execute("ALTER TABLE fcx_migration_user_map ALTER COLUMN ravenhood_internal_id DROP NOT NULL")
    target.commit()


def bootstrap_target_schema(target: psycopg.Connection[Any]) -> None:
    """Create only the target-owned prerequisites for an empty FCX database."""
    with target.cursor() as cursor:
        for statement in TARGET_BOOTSTRAP_DDL:
            cursor.execute(statement)
    target.commit()


def source_fingerprint(source: psycopg.Connection[Any], tables: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for table in tables:
        count = row_count(source, "public", table, settings_only=(table == "system_settings"))
        digest.update(f"{table}:{count}\n".encode())
    return digest.hexdigest()


def ensure_stage_table(
    source: psycopg.Connection[Any], target: psycopg.Connection[Any], table: str
) -> list[str]:
    source_columns = columns(source, "public", table)
    if not source_columns:
        raise RuntimeError(f"Source table {table} has no columns")
    definitions = [
        sql.SQL("{} {}").format(safe_ident(str(item["name"])), sql.SQL(str(item["data_type"])))
        for item in source_columns
    ]
    primary_key = primary_key_columns(source, "public", table)
    if primary_key:
        definitions.append(
            sql.SQL("PRIMARY KEY ({})").format(sql.SQL(",").join(map(safe_ident, primary_key)))
        )
    with target.cursor() as cursor:
        cursor.execute(
            sql.SQL("DROP TABLE IF EXISTS {}.{} CASCADE").format(safe_ident(STAGE_SCHEMA), safe_ident(table))
        )
        cursor.execute(
            sql.SQL("CREATE TABLE {}.{} ({})").format(
                safe_ident(STAGE_SCHEMA), safe_ident(table), sql.SQL(",").join(definitions)
            )
        )
    return [str(item["name"]) for item in source_columns]


def stage_table(
    source: psycopg.Connection[Any], target: psycopg.Connection[Any], table: str
) -> int:
    names = ensure_stage_table(source, target, table)
    source_query = sql.SQL("SELECT {} FROM public.{}").format(
        sql.SQL(",").join(map(safe_ident, names)), safe_ident(table)
    )
    if table == "system_settings":
        source_query += sql.SQL(
            " WHERE setting_key LIKE 'market_%' OR setting_key LIKE 'fcx_%' OR setting_key LIKE 'business_ipo_%' OR setting_key LIKE 'business_issuer_%'"
        )
    target_copy = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        safe_ident(STAGE_SCHEMA), safe_ident(table), sql.SQL(",").join(map(safe_ident, names))
    )
    copied = 0
    cursor_name = "cad1_" + hashlib.sha1(table.encode()).hexdigest()[:12]
    with source.cursor(name=cursor_name) as source_cursor, target.cursor() as target_cursor:
        source_cursor.execute(source_query)
        with target_cursor.copy(target_copy) as writer:
            while True:
                batch = source_cursor.fetchmany(2_000)
                if not batch:
                    break
                for row in batch:
                    writer.write_row(row)
                copied += len(batch)
    return copied


def ensure_operational_table(
    source: psycopg.Connection[Any], target: psycopg.Connection[Any], table: str
) -> None:
    source_columns = columns(source, "public", table)
    if not table_exists(target, "public", table):
        definitions: list[sql.Composable] = []
        for item in source_columns:
            definition = sql.SQL("{} {}").format(
                safe_ident(str(item["name"])), sql.SQL(str(item["data_type"]))
            )
            default = str(item.get("column_default") or "")
            identity_kind = str(item.get("identity_kind") or "")
            if identity_kind:
                definition += sql.SQL(" GENERATED {} AS IDENTITY").format(
                    sql.SQL("ALWAYS" if identity_kind == "a" else "BY DEFAULT")
                )
            elif default.startswith("nextval("):
                # Source SERIAL sequences belong to CAD 1.  Create an independent
                # FCX-owned sequence rather than copying a cross-database default.
                definition += sql.SQL(" GENERATED BY DEFAULT AS IDENTITY")
            elif default:
                definition += sql.SQL(" DEFAULT ") + sql.SQL(default)
            if bool(item.get("not_null")):
                definition += sql.SQL(" NOT NULL")
            definitions.append(definition)
        pk = primary_key_columns(source, "public", table)
        if pk:
            definitions.append(sql.SQL("PRIMARY KEY ({})").format(sql.SQL(",").join(map(safe_ident, pk))))
        with target.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE TABLE public.{} ({})").format(safe_ident(table), sql.SQL(",").join(definitions))
            )
        return
    target_names = {str(item["name"]) for item in columns(target, "public", table)}
    with target.cursor() as cursor:
        for item in source_columns:
            name = str(item["name"])
            if name not in target_names:
                cursor.execute(
                    sql.SQL("ALTER TABLE public.{} ADD COLUMN {} {}").format(
                        safe_ident(table), safe_ident(name), sql.SQL(str(item["data_type"]))
                    )
                )


def relevant_source_users(source: psycopg.Connection[Any], tables: set[str]) -> tuple[set[int], set[int]]:
    players: set[int] = set()
    admins: set[int] = set()
    for table, names in PLAYER_USER_COLUMNS.items():
        if table not in tables:
            continue
        available = {str(item["name"]) for item in columns(source, "public", table)}
        for name in names:
            if name not in available:
                continue
            with source.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT DISTINCT {} FROM public.{} WHERE {} IS NOT NULL").format(
                        safe_ident(name), safe_ident(table), safe_ident(name)
                    )
                )
                players.update(int(row[0]) for row in cursor.fetchall())
    for table, names in ADMIN_USER_COLUMNS.items():
        if table not in tables:
            continue
        available = {str(item["name"]) for item in columns(source, "public", table)}
        for name in names:
            if name not in available:
                continue
            with source.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT DISTINCT {} FROM public.{} WHERE {} IS NOT NULL").format(
                        safe_ident(name), safe_ident(table), safe_ident(name)
                    )
                )
                admins.update(int(row[0]) for row in cursor.fetchall())
    return players, admins


def deterministic_account_id(source_user_id: int, identity_id: str) -> str:
    material = f"bohemia:{identity_id.strip().lower()}" if identity_id.strip() else f"{COMMUNITY_ID}:user:{source_user_id}"
    return "RH-" + hashlib.sha256(material.encode()).hexdigest()[:24].upper()


def migrate_users(
    source: psycopg.Connection[Any], target: psycopg.Connection[Any], run_id: str, tables: set[str]
) -> tuple[dict[int, int], dict[int, int]]:
    player_ids, admin_ids = relevant_source_users(source, tables)
    all_ids = sorted(player_ids | admin_ids)
    if not all_ids:
        return {}, {}
    with source.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT u.id,u.name,u.email,u.civ_number,u.verified,
                      COALESCE(l.identity_id,'') AS identity_id,
                      COALESCE(l.player_name,'') AS player_name
               FROM users u LEFT JOIN arma_account_links l ON l.user_id=u.id
               WHERE u.id = ANY(%s) ORDER BY u.id""",
            (all_ids,),
        )
        source_users = {int(row["id"]): dict(row) for row in cursor.fetchall()}
    missing = sorted(set(all_ids) - set(source_users))
    if missing:
        raise RuntimeError(f"Referenced CAD 1 users are missing: {missing[:20]}")

    identity_owners: dict[str, list[int]] = {}
    for source_id in sorted(player_ids):
        identity = str(source_users[source_id].get("identity_id") or "").strip().lower()
        if identity:
            identity_owners.setdefault(identity, []).append(source_id)
    duplicate_identities = {
        identity: owners for identity, owners in identity_owners.items() if len(owners) > 1
    }
    if duplicate_identities:
        preview = ", ".join(
            f"{identity}=>{owners}" for identity, owners in list(duplicate_identities.items())[:10]
        )
        raise RuntimeError(
            "Migration refused: multiple CAD 1 users resolve to the same Bohemia identity: " + preview
        )

    ravenhood_map: dict[int, int] = {}
    admin_map: dict[int, int] = {}
    now = utcnow()
    with target.cursor(row_factory=dict_row) as cursor:
        for source_id in all_ids:
            user = source_users[source_id]
            identity = str(user.get("identity_id") or "")
            display = str(user.get("name") or user.get("player_name") or f"CAD 1 User {source_id}")
            account_id: str | None = None
            ravenhood_internal: int | None = None
            if source_id in player_ids:
                account_id = deterministic_account_id(source_id, identity)
                cursor.execute(
                    """SELECT account_id FROM fcx_ravenhood_links
                       WHERE bohemia_identity_id=%s AND verified=TRUE LIMIT 1""",
                    (identity,),
                ) if identity else None
                linked = cursor.fetchone() if identity else None
                if linked:
                    account_id = str(linked["account_id"])
                cursor.execute(
                    """INSERT INTO fcx_ravenhood_accounts
                           (account_id,display_name,status,created_at,updated_at)
                       VALUES (%s,%s,'active',%s,%s)
                       ON CONFLICT(account_id) DO UPDATE SET
                           display_name=EXCLUDED.display_name,updated_at=EXCLUDED.updated_at
                       RETURNING id""",
                    (account_id, display, now, now),
                )
                ravenhood_internal = int(cursor.fetchone()["id"])
                ravenhood_map[source_id] = ravenhood_internal
                cursor.execute(
                    """INSERT INTO fcx_ravenhood_links
                           (account_id,community_id,community_user_id,bohemia_identity_id,verified,active,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s)
                       ON CONFLICT(community_id,community_user_id) DO UPDATE SET
                           account_id=EXCLUDED.account_id,
                           bohemia_identity_id=EXCLUDED.bohemia_identity_id,
                           verified=EXCLUDED.verified,active=TRUE,updated_at=EXCLUDED.updated_at""",
                    (account_id, COMMUNITY_ID, str(source_id), identity, bool(user.get("verified") or identity), now, now),
                )
            admin_internal: int | None = None
            if source_id in admin_ids:
                placeholder_email = f"cad1-actor-{source_id}@migration.invalid"
                cursor.execute(
                    """INSERT INTO fcx_control_admin_users
                           (email,display_name,password_hash,roles_json,active,source_cad,source_user_id,created_at,updated_at)
                       VALUES (%s,%s,%s,'[\"imported_actor\"]'::jsonb,FALSE,%s,%s,%s,%s)
                       ON CONFLICT(email) DO UPDATE SET
                           display_name=EXCLUDED.display_name,source_cad=EXCLUDED.source_cad,
                           source_user_id=EXCLUDED.source_user_id,updated_at=EXCLUDED.updated_at
                       RETURNING id""",
                    (placeholder_email, display, "!migration-no-login!", COMMUNITY_ID, str(source_id), now, now),
                )
                admin_internal = int(cursor.fetchone()["id"])
                admin_map[source_id] = admin_internal
            cursor.execute(
                """INSERT INTO fcx_migration_user_map
                       (run_id,source_user_id,source_display_name,source_email,bohemia_identity_id,
                        ravenhood_account_id,ravenhood_internal_id,admin_internal_id,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(run_id,source_user_id) DO UPDATE SET
                       source_display_name=EXCLUDED.source_display_name,
                       source_email=EXCLUDED.source_email,
                       bohemia_identity_id=EXCLUDED.bohemia_identity_id,
                       ravenhood_account_id=EXCLUDED.ravenhood_account_id,
                       ravenhood_internal_id=EXCLUDED.ravenhood_internal_id,
                       admin_internal_id=EXCLUDED.admin_internal_id""",
                (run_id, source_id, display, str(user.get("email") or ""), identity, account_id,
                 ravenhood_internal, admin_internal, now),
            )
    return ravenhood_map, admin_map


def remap_value(table: str, column: str, value: Any, players: dict[int, int], admins: dict[int, int]) -> Any:
    if value is None:
        return None
    if column in PLAYER_USER_COLUMNS.get(table, ()):
        source_id = int(value)
        if source_id not in players:
            raise RuntimeError(f"No Ravenhood identity mapping for {table}.{column}={source_id}")
        return players[source_id]
    if column in ADMIN_USER_COLUMNS.get(table, ()):
        source_id = int(value)
        if source_id not in admins:
            raise RuntimeError(f"No FCX actor mapping for {table}.{column}={source_id}")
        return admins[source_id]
    return value


def promote_table(
    source: psycopg.Connection[Any],
    target: psycopg.Connection[Any],
    table: str,
    player_map: dict[int, int],
    admin_map: dict[int, int],
) -> int:
    ensure_operational_table(source, target, table)
    source_names = [str(item["name"]) for item in columns(source, "public", table)]
    target_names = {str(item["name"]) for item in columns(target, "public", table)}
    names = [name for name in source_names if name in target_names]
    pk = [name for name in primary_key_columns(source, "public", table) if name in names]
    if table == "system_settings":
        pk = ["setting_key"]
    select_query = sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(",").join(map(safe_ident, names)), safe_ident(STAGE_SCHEMA), safe_ident(table)
    )
    placeholders = sql.SQL(",").join(sql.Placeholder() for _ in names)
    insert_query = sql.SQL("INSERT INTO public.{} ({}) VALUES ({})").format(
        safe_ident(table), sql.SQL(",").join(map(safe_ident, names)), placeholders
    )
    if pk:
        updates = [name for name in names if name not in pk]
        insert_query += sql.SQL(" ON CONFLICT ({}) ").format(sql.SQL(",").join(map(safe_ident, pk)))
        if updates:
            insert_query += sql.SQL("DO UPDATE SET ") + sql.SQL(",").join(
                sql.SQL("{}=EXCLUDED.{}").format(safe_ident(name), safe_ident(name)) for name in updates
            )
        else:
            insert_query += sql.SQL("DO NOTHING")
    promoted = 0
    with target.cursor(row_factory=dict_row) as read_cursor, target.cursor() as write_cursor:
        read_cursor.execute(select_query)
        while True:
            rows = read_cursor.fetchmany(1_000)
            if not rows:
                break
            values: list[tuple[Any, ...]] = []
            for row in rows:
                values.append(
                    tuple(remap_value(table, name, row[name], player_map, admin_map) for name in names)
                )
            write_cursor.executemany(insert_query, values)
            promoted += len(values)
    return promoted


def reset_sequences(target: psycopg.Connection[Any], tables: Iterable[str]) -> None:
    with target.cursor() as cursor:
        for table in tables:
            if not table_exists(target, "public", table):
                continue
            target_columns = {str(item["name"]) for item in columns(target, "public", table)}
            if "id" not in target_columns:
                continue
            cursor.execute("SELECT pg_get_serial_sequence(%s,'id')", (f"public.{table}",))
            sequence = cursor.fetchone()[0]
            if not sequence:
                continue
            cursor.execute(sql.SQL("SELECT COALESCE(MAX(id),0) FROM public.{}").format(safe_ident(table)))
            maximum = int(cursor.fetchone()[0])
            cursor.execute("SELECT setval(%s,%s,%s)", (sequence, max(1, maximum), maximum > 0))


def decimal_sum(connection: psycopg.Connection[Any], schema: str, table: str, column: str) -> Decimal:
    available = {str(item["name"]) for item in columns(connection, schema, table)}
    if column not in available:
        return Decimal("0")
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT COALESCE(SUM({}),0) FROM {}.{}").format(
                safe_ident(column), safe_ident(schema), safe_ident(table)
            )
        )
        return Decimal(str(cursor.fetchone()[0] or 0))


def imported_decimal_sum(
    connection: psycopg.Connection[Any], table: str, column: str, primary_key: Sequence[str]
) -> Decimal:
    """Sum only operational rows represented in this run's staging table."""
    if not primary_key:
        raise RuntimeError(f"Cannot scope financial reconciliation for {table}: no primary key")
    join = sql.SQL(" AND ").join(
        sql.SQL("p.{}=s.{}").format(safe_ident(name), safe_ident(name)) for name in primary_key
    )
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT COALESCE(SUM(p.{}),0) FROM public.{} p JOIN {}.{} s ON {}").format(
                safe_ident(column), safe_ident(table), safe_ident(STAGE_SCHEMA), safe_ident(table), join
            )
        )
        return Decimal(str(cursor.fetchone()[0] or 0))


def verify_operational_relationships(
    source: psycopg.Connection[Any], target: psycopg.Connection[Any], tables: Sequence[str]
) -> dict[str, Any]:
    """Verify every imported FCX-to-FCX foreign key resolves after promotion."""
    table_set = set(tables)
    report: dict[str, Any] = {}
    for foreign_key in source_foreign_keys(source, tables):
        child_table = str(foreign_key["child_table"])
        parent_table = str(foreign_key["parent_table"])
        if parent_table not in table_set:
            # CAD users are intentionally remapped to standalone FCX identity tables.
            continue
        child_columns = [str(item) for item in foreign_key["child_columns"]]
        parent_columns = [str(item) for item in foreign_key["parent_columns"]]
        child_pk = primary_key_columns(target, STAGE_SCHEMA, child_table)
        if not child_pk and "id" in {
            str(item["name"]) for item in columns(target, STAGE_SCHEMA, child_table)
        }:
            child_pk = ["id"]
        if not child_pk:
            raise RuntimeError(
                f"Cannot scope relationship reconciliation for {child_table}: no primary key"
            )
        imported_join = sql.SQL(" AND ").join(
            sql.SQL("c.{}=s.{}").format(safe_ident(name), safe_ident(name))
            for name in child_pk
        )
        parent_join = sql.SQL(" AND ").join(
            sql.SQL("c.{}=p.{}").format(safe_ident(child), safe_ident(parent))
            for child, parent in zip(child_columns, parent_columns)
        )
        reference_present = sql.SQL(" AND ").join(
            sql.SQL("c.{} IS NOT NULL").format(safe_ident(name)) for name in child_columns
        )
        with target.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT COUNT(*) FROM public.{} c "
                    "JOIN {}.{} s ON {} "
                    "LEFT JOIN public.{} p ON {} "
                    "WHERE {} AND p.{} IS NULL"
                ).format(
                    safe_ident(child_table),
                    safe_ident(STAGE_SCHEMA),
                    safe_ident(child_table),
                    imported_join,
                    safe_ident(parent_table),
                    parent_join,
                    reference_present,
                    safe_ident(parent_columns[0]),
                )
            )
            orphan_count = int(cursor.fetchone()[0])
        key = f"{child_table}.{','.join(child_columns)}->{parent_table}.{','.join(parent_columns)}"
        report[key] = {"orphans": orphan_count, "ok": orphan_count == 0}
    return report


def verify(
    source: psycopg.Connection[Any], target: psycopg.Connection[Any], run_id: str,
    tables: Sequence[str], expected_counts: dict[str, int],
    expected_resident_ids: set[int], expected_audit_actor_ids: set[int],
) -> dict[str, Any]:
    report: dict[str, Any] = {"tables": {}, "financial_totals": {}, "ok": True}
    for table in tables:
        source_rows = expected_counts[table]
        staged_rows = row_count(target, STAGE_SCHEMA, table)
        pk = primary_key_columns(target, STAGE_SCHEMA, table)
        if not pk and "id" in {str(item["name"]) for item in columns(target, STAGE_SCHEMA, table)}:
            pk = ["id"]
        if pk:
            join = sql.SQL(" AND ").join(
                sql.SQL("p.{}=s.{}").format(safe_ident(name), safe_ident(name)) for name in pk
            )
            with target.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT COUNT(*) FROM public.{} p JOIN {}.{} s ON {}").format(
                        safe_ident(table), safe_ident(STAGE_SCHEMA), safe_ident(table), join
                    )
                )
                promoted_rows = int(cursor.fetchone()[0])
        else:
            promoted_rows = row_count(target, "public", table)
        table_ok = source_rows == staged_rows == promoted_rows
        report["tables"][table] = {
            "source": source_rows,
            "staged": staged_rows,
            "promoted": promoted_rows,
            "ok": table_ok,
        }
        report["ok"] = bool(report["ok"] and table_ok)
        if table in MONEY_CHECKS:
            table_totals: dict[str, Any] = {}
            for column in MONEY_CHECKS[table]:
                staged_total = decimal_sum(target, STAGE_SCHEMA, table, column)
                operational_total = imported_decimal_sum(target, table, column, pk)
                total_ok = staged_total == operational_total
                table_totals[column] = {
                    "staged": str(staged_total),
                    "operational": str(operational_total),
                    "ok": total_ok,
                }
                report["ok"] = bool(report["ok"] and total_ok)
            report["financial_totals"][table] = table_totals
    with target.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM fcx_migration_user_map WHERE run_id=%s", (run_id,))
        report["identity_mappings"] = int(cursor.fetchone()[0])
        cursor.execute(
            """SELECT COUNT(*) FILTER (WHERE ravenhood_internal_id IS NOT NULL),
                      COUNT(*) FILTER (WHERE admin_internal_id IS NOT NULL)
               FROM fcx_migration_user_map WHERE run_id=%s""",
            (run_id,),
        )
        identity_counts = cursor.fetchone()
        report["resident_identity_mappings"] = int(identity_counts[0])
        report["audit_actor_mappings"] = int(identity_counts[1])
    expected_all_ids = expected_resident_ids | expected_audit_actor_ids
    report["identity_mapping_expectations"] = {
        "all_referenced_users": len(expected_all_ids),
        "resident_users": len(expected_resident_ids),
        "audit_actor_users": len(expected_audit_actor_ids),
    }
    identity_ok = (
        report["identity_mappings"] == len(expected_all_ids)
        and report["resident_identity_mappings"] == len(expected_resident_ids)
        and report["audit_actor_mappings"] == len(expected_audit_actor_ids)
    )
    report["identity_mappings_ok"] = identity_ok
    report["ok"] = bool(report["ok"] and identity_ok)
    relationships = verify_operational_relationships(source, target, tables)
    report["relationships"] = relationships
    report["ok"] = bool(
        report["ok"] and all(item["ok"] for item in relationships.values())
    )
    return report


def upsert_run(
    target: psycopg.Connection[Any], run_id: str, *, phase: str, status: str,
    fingerprint: str = "", table_count: int = 0, row_count_value: int = 0,
    report: dict[str, Any] | None = None, failure: str = "",
) -> None:
    now = utcnow()
    completed = now if status in {"verified", "cutover_ready", "failed"} else None
    with target.cursor() as cursor:
        cursor.execute(
            """INSERT INTO fcx_migration_runs
                   (run_id,source_name,community_id,phase,status,source_fingerprint,
                    table_count,row_count,reconciliation_json,started_at,updated_at,completed_at,failure_message)
               VALUES (%s,'CAD 1',%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
               ON CONFLICT(run_id) DO UPDATE SET
                   phase=EXCLUDED.phase,status=EXCLUDED.status,
                   source_fingerprint=CASE WHEN EXCLUDED.source_fingerprint<>'' THEN EXCLUDED.source_fingerprint ELSE fcx_migration_runs.source_fingerprint END,
                   table_count=GREATEST(fcx_migration_runs.table_count,EXCLUDED.table_count),
                   row_count=GREATEST(fcx_migration_runs.row_count,EXCLUDED.row_count),
                   reconciliation_json=CASE WHEN EXCLUDED.reconciliation_json<>'{}'::jsonb THEN EXCLUDED.reconciliation_json ELSE fcx_migration_runs.reconciliation_json END,
                   updated_at=EXCLUDED.updated_at,completed_at=EXCLUDED.completed_at,
                   failure_message=EXCLUDED.failure_message""",
            (run_id, COMMUNITY_ID, phase, status, fingerprint, table_count, row_count_value,
             json.dumps(report or {}, default=str), now, now, completed, failure[:4000]),
        )


def make_run_id(fingerprint: str) -> str:
    return f"cad1-{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{fingerprint[:10]}-{secrets.token_hex(3)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("plan", "copy", "verify", "finalize", "all"), default="plan")
    parser.add_argument("--run-id", default=os.environ.get("FCX_MIGRATION_RUN_ID", "").strip())
    parser.add_argument(
        "--allow-target-merge",
        action="store_true",
        help="Permit an explicitly reviewed merge into non-empty FCX operational tables",
    )
    parser.add_argument(
        "--bootstrap-target",
        action="store_true",
        help=(
            "Initialize the minimal standalone FCX identity/control schema in an empty "
            "target database before preflight. This never writes to CAD 1."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit the final report as JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_url = required_env("SOURCE_CAD1_DATABASE_URL")
    target_url = required_env("FCX_DATABASE_URL")
    if source_url == target_url:
        raise RuntimeError("Source CAD 1 and target FCX database URLs must be different")

    with psycopg.connect(source_url, autocommit=False) as source, psycopg.connect(target_url, autocommit=False) as target:
        # Enforce the most important safety invariant at the database layer.
        source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        ensure_migration_schema(target)
        if args.bootstrap_target:
            bootstrap_target_schema(target)
        tables = fetch_tables(source)
        if not tables:
            raise RuntimeError("No Ravenhood/FCX source tables were discovered in CAD 1")
        tables.sort(key=lambda item: (TABLE_PRIORITY.get(item, 1_000), item))
        user_reference_registry = validate_user_reference_registry(source, tables)
        expected_resident_ids, expected_audit_actor_ids = relevant_source_users(
            source, set(tables)
        )
        operational_relationships = [
            {
                "constraint": str(item["constraint_name"]),
                "child_table": str(item["child_table"]),
                "child_columns": list(item["child_columns"]),
                "parent_table": str(item["parent_table"]),
                "parent_columns": list(item["parent_columns"]),
            }
            for item in source_foreign_keys(source, tables)
            if str(item["parent_table"]) in set(tables)
        ]
        preflight = target_preflight(source, target, tables)
        expected_counts = {
            table: row_count(source, "public", table, settings_only=(table == "system_settings"))
            for table in tables
        }
        fingerprint = source_fingerprint(source, tables)
        run_id = args.run_id or make_run_id(fingerprint)
        plan = {
            "run_id": run_id,
            "community_id": COMMUNITY_ID,
            "source_fingerprint": fingerprint,
            "tables": expected_counts,
            "table_count": len(tables),
            "row_count": sum(expected_counts.values()),
            "source_read_only": True,
            "user_reference_registry": user_reference_registry,
            "identity_expectations": {
                "all_referenced_users": len(expected_resident_ids | expected_audit_actor_ids),
                "resident_users": len(expected_resident_ids),
                "audit_actor_users": len(expected_audit_actor_ids),
            },
            "operational_relationships": operational_relationships,
            "preflight": preflight,
        }
        compact_plan = {
            "run_id": run_id,
            "phase": args.phase,
            "community_id": COMMUNITY_ID,
            "source_fingerprint": fingerprint,
            "table_count": len(tables),
            "row_count": sum(expected_counts.values()),
            "source_read_only": True,
            "identity_expectations": plan["identity_expectations"],
            "same_database": bool(preflight["same_database"]),
            "missing_required_target_tables": preflight["missing_required_target_tables"],
            "target_nonempty_operational_tables": preflight["target_nonempty_operational_tables"],
            "tables": expected_counts,
        }
        if args.phase == "plan":
            print(json.dumps(plan, indent=2, default=str) if args.json else json.dumps(compact_plan, default=str))
            return 0

        if preflight["same_database"]:
            raise RuntimeError("Migration refused: CAD 1 and FCX resolve to the same PostgreSQL database")
        if preflight["missing_required_target_tables"]:
            missing = ", ".join(preflight["missing_required_target_tables"])
            raise RuntimeError(
                f"Migration refused: initialize the standalone FCX schema first; missing {missing}"
            )
        if preflight["target_nonempty_operational_tables"] and not args.allow_target_merge:
            occupied = ", ".join(
                f"{table}={count}"
                for table, count in preflight["target_nonempty_operational_tables"].items()
            )
            raise RuntimeError(
                "Migration refused: target FCX already has operational rows. Review the plan and "
                f"rerun with --allow-target-merge only if intentional. Existing rows: {occupied}"
            )

        try:
            upsert_run(target, run_id, phase="stage", status="running", fingerprint=fingerprint,
                       table_count=len(tables), row_count_value=sum(expected_counts.values()))
            target.commit()
            if args.phase in {"copy", "all"}:
                for table in tables:
                    copied = stage_table(source, target, table)
                    with target.cursor() as cursor:
                        cursor.execute(
                            """INSERT INTO fcx_migration_table_results
                                   (run_id,table_name,source_rows,staged_rows,promoted_rows,status,updated_at)
                               VALUES (%s,%s,%s,%s,0,'staged',%s)
                               ON CONFLICT(run_id,table_name) DO UPDATE SET
                                   source_rows=EXCLUDED.source_rows,staged_rows=EXCLUDED.staged_rows,
                                   status='staged',updated_at=EXCLUDED.updated_at""",
                            (run_id, table, expected_counts[table], copied, utcnow()),
                        )
                    target.commit()

                player_map, admin_map = migrate_users(source, target, run_id, set(tables))
                target.commit()
                for table in tables:
                    promoted = promote_table(source, target, table, player_map, admin_map)
                    with target.cursor() as cursor:
                        cursor.execute(
                            """UPDATE fcx_migration_table_results
                               SET promoted_rows=%s,status='promoted',updated_at=%s
                               WHERE run_id=%s AND table_name=%s""",
                            (promoted, utcnow(), run_id, table),
                        )
                    target.commit()
                reset_sequences(target, tables)
                upsert_run(target, run_id, phase="promote", status="copied")
                target.commit()

            if args.phase in {"verify", "all"}:
                report = verify(
                    source, target, run_id, tables, expected_counts,
                    expected_resident_ids, expected_audit_actor_ids,
                )
                upsert_run(target, run_id, phase="verify", status="verified" if report["ok"] else "failed", report=report)
                target.commit()
                if not report["ok"]:
                    print(json.dumps(report, indent=2, default=str))
                    return 2
            else:
                report = {}

            if args.phase in {"finalize", "all"}:
                if not report:
                    report = verify(
                        source, target, run_id, tables, expected_counts,
                        expected_resident_ids, expected_audit_actor_ids,
                    )
                if not report.get("ok"):
                    raise RuntimeError("Cutover refused because reconciliation is not clean")
                now_text = utcnow().isoformat()
                with target.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO system_settings(setting_key,setting_value,updated_at)
                           VALUES ('fcx_cad1_migration_cutover_ready',%s,%s)
                           ON CONFLICT(setting_key) DO UPDATE SET
                               setting_value=EXCLUDED.setting_value,updated_at=EXCLUDED.updated_at""",
                        (run_id, now_text),
                    )
                upsert_run(target, run_id, phase="finalize", status="cutover_ready", report=report)
                target.commit()

            result = {**plan, "status": "cutover_ready" if args.phase in {"finalize", "all"} else "copied", "reconciliation": report}
            compact_result = {
                **compact_plan,
                "status": result["status"],
                "reconciliation": report,
            }
            print(json.dumps(result, indent=2, default=str) if args.json else json.dumps(compact_result, default=str))
            return 0
        except Exception as exc:
            target.rollback()
            try:
                upsert_run(target, run_id, phase=args.phase, status="failed", fingerprint=fingerprint, failure=f"{type(exc).__name__}: {exc}")
                target.commit()
            except Exception:
                target.rollback()
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
