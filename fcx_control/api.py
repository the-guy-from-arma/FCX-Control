from __future__ import annotations

import json
import hashlib
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from fcx_engine.service import (
    CycleRequest as EngineCycleRequest,
    DividendRequest as EngineDividendRequest,
    EngineSettingsRequest,
    KillSwitchRequest as EngineKillSwitchRequest,
    SandboxRequest as EngineSandboxRequest,
    SeedRequest as EngineSeedRequest,
    StockSplitRequest as EngineStockSplitRequest,
    admin_cycle as run_engine_cycle,
    admin_dividend as run_engine_dividend,
    admin_kill_switch as set_engine_kill_switch,
    admin_market_pause as pause_engine_market,
    admin_market_resume as resume_engine_market,
    admin_market_snapshot as engine_market_snapshot,
    admin_sandbox as run_engine_sandbox,
    admin_seed as seed_engine_population,
    admin_settings as update_engine_configuration,
    admin_stock_split as run_engine_stock_split,
    admin_ticker_pause as pause_engine_ticker,
    admin_ticker_resume as resume_engine_ticker,
)

from .config import settings
from .db import all_rows, execute, one, transaction
from .indexes import market_cap_weights, rank_by_market_cap, security_market_cap
from .security import (
    community_principal,
    create_session,
    current_admin,
    new_api_credential,
    protected_hash,
    require_admin_csrf,
    require_community_scopes,
    require_csrf,
    require_roles,
    verify_password,
    hash_password,
)


router = APIRouter(prefix="/api/v1")
ADMIN_ROLES = ("developer", "commissioner", "fec_admin", "fcx_admin")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def audit(
    connection: Any,
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    actor_role: str = "",
    previous: Any = None,
    new: Any = None,
    reason: str = "",
    request: Request | None = None,
) -> None:
    execute(
        connection,
        """INSERT INTO fcx_audit_log
           (actor_type,actor_id,actor_role,action,target_type,target_id,previous_json,
            new_json,reason,ip_address,session_id,created_at)
           VALUES (:actor_type,:actor_id,:actor_role,:action,:target_type,:target_id,
            CAST(:previous AS jsonb),CAST(:new AS jsonb),:reason,:ip,:session,:now)""",
        {
            "actor_type": actor_type,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "previous": json.dumps(previous or {}, default=str, separators=(",", ":")),
            "new": json.dumps(new or {}, default=str, separators=(",", ":")),
            "reason": reason,
            "ip": request.client.host if request and request.client else "",
            "session": "",
            "now": utcnow(),
        },
    )


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)


class CommunityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    community_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    community_name: str = Field(min_length=2, max_length=120)
    bank_bridge_url: str = Field(default="", max_length=2000)
    bank_secret_env: str = Field(default="", pattern=r"^[A-Z0-9_]*$", max_length=120)


class CommunityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    community_name: str | None = Field(default=None, min_length=2, max_length=120)
    status: Literal["active", "disabled", "terminated"] | None = None
    connection_enabled: bool | None = None
    trading_enabled: bool | None = None
    buy_enabled: bool | None = None
    sell_enabled: bool | None = None
    account_creation_enabled: bool | None = None
    suspended: bool | None = None
    suspension_reason: str | None = Field(default=None, max_length=2000)
    bank_bridge_url: str | None = Field(default=None, max_length=2000)
    bank_secret_env: str | None = Field(default=None, pattern=r"^[A-Z0-9_]*$", max_length=120)
    scheduled_open: datetime | None = None
    scheduled_close: datetime | None = None


class CredentialRequest(BaseModel):
    scopes: list[str] = Field(default_factory=lambda: ["market:read", "account:link", "trade:write", "settlement:write"])


class ResolveAccountRequest(BaseModel):
    community_user_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=200)
    bohemia_identity_id: str = Field(default="", max_length=200)
    verified: bool = False


class SettlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=200)
    account_id: str = Field(min_length=4, max_length=200)
    community_user_id: str = Field(min_length=1, max_length=200)
    operation: Literal["debit", "credit"]
    amount: Decimal = Field(gt=0, max_digits=24, decimal_places=2)
    currency: str = Field(default="FC", pattern=r"^[A-Z0-9_-]{2,12}$")
    order_reference: str = Field(default="", max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SettlementCallback(BaseModel):
    state: Literal["BANK_AUTHORIZED", "BANK_DEBITED", "BANK_CREDITED", "ORDER_EXECUTED", "SETTLED", "FAILED", "REVERSED"]
    bank_reference: str = Field(default="", max_length=200)
    failure_code: str = Field(default="", max_length=120)
    failure_message: str = Field(default="", max_length=2000)
    response: dict[str, Any] = Field(default_factory=dict)


class SettlementAdminAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="Operator action", max_length=1000)


class SettlementBulkAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settlement_ids: list[str] = Field(default_factory=list, max_length=500)
    community_id: str = Field(default="", max_length=120)
    reason: str = Field(default="Bulk operator action", max_length=1000)


class TradeOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=200)
    community_user_id: str = Field(min_length=1, max_length=200)
    account_id: str = Field(min_length=4, max_length=200)
    ticker: str = Field(pattern=r"^[A-Za-z0-9._-]{1,16}$")
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0, max_digits=24, decimal_places=8)


class InvestigationRequest(BaseModel):
    account_id: str = Field(min_length=4, max_length=200)
    summary: str = Field(min_length=10, max_length=4000)
    priority: Literal["low", "normal", "high", "critical"] = "normal"


class InvestigationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["open", "review", "restricted", "closed"] | None = None
    priority: Literal["low", "normal", "high", "critical"] | None = None
    summary: str | None = Field(default=None, min_length=10, max_length=4000)
    notes_json: list[dict[str, Any]] | None = None


class MarketSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market_open: bool | None = None
    buy_enabled: bool | None = None
    sell_enabled: bool | None = None
    account_creation_enabled: bool | None = None
    maintenance_mode: bool | None = None
    leverage_enabled: bool | None = None
    max_leverage: Decimal | None = Field(default=None, ge=1, le=200)
    manual_override: Literal["schedule", "open", "closed"] | None = None
    schedule_open_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    schedule_close_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    schedule_timezone: str | None = Field(default=None, min_length=3, max_length=80)
    weekends_enabled: bool | None = None
    fcxv_24h_enabled: bool | None = None
    transfer_fee_percent: Decimal | None = Field(default=None, ge=0, le=25)
    trade_fee_percent: Decimal | None = Field(default=None, ge=0, le=10)
    margin_enabled: bool | None = None
    margin_maintenance_percent: Decimal | None = Field(default=None, ge=5, le=80)
    margin_max_open_positions: int | None = Field(default=None, ge=1, le=25)
    margin_max_account_notional: Decimal | None = Field(default=None, ge=100, le=1_000_000_000)
    liquidation_hunts_enabled: bool | None = None
    liquidation_hunt_threshold: Decimal | None = Field(default=None, ge=0, le=1_000_000_000_000_000)
    liquidation_hunt_probability_percent: Decimal | None = Field(default=None, ge=0, le=100)
    liquidation_hunt_intensity: Literal["light", "balanced", "aggressive", "extreme"] | None = None
    liquidation_hunt_max_move_percent: Decimal | None = Field(default=None, ge=0.01, le=1000)
    liquidation_hunt_cooldown_minutes: int | None = Field(default=None, ge=1, le=10080)
    liquidation_hunt_market_hours_only: bool | None = None


class PriceProgramRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    security_ids: list[int] = Field(min_length=1, max_length=100)
    event_name: str = Field(min_length=3, max_length=500)
    percent_change: Decimal = Field(ge=-99.99, le=1000, max_digits=12, decimal_places=4)
    duration_minutes: int = Field(ge=1, le=43200)
    starts_at: datetime | None = None


class ProgramStopRequest(BaseModel):
    keep_current_price: bool = True


class SecurityHaltRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    security_ids: list[int] = Field(min_length=1, max_length=100)
    reason_code: str = Field(default="FEC_REVIEW", min_length=2, max_length=120)
    reason_label: str = Field(default="FEC market-integrity review", min_length=3, max_length=300)
    public_notice: str = Field(default="Trading temporarily halted by the FEC.", max_length=2000)
    case_reference: str = Field(default="", max_length=200)
    automatic_resume_at: datetime | None = None


class SecurityDelistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    security_id: int = Field(ge=1)
    reason_code: str = Field(default="FEC_ORDER", min_length=2, max_length=120)
    reason_label: str = Field(default="FEC listing order", min_length=3, max_length=300)
    public_notice: str = Field(default="Security removed from public FCX trading by FEC order.", max_length=2000)
    case_reference: str = Field(default="", max_length=200)


class ReleaseRequest(BaseModel):
    note: str = Field(default="Released by authorized FEC operator", max_length=2000)


class AccountRestrictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int = Field(ge=1)
    scope: Literal["full", "equity", "leverage"]
    reason: str = Field(min_length=3, max_length=2000)
    case_reference: str = Field(default="", max_length=200)


class AssetSeizureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int = Field(ge=1)
    amount: Decimal = Field(gt=0, le=50_000_000_000, max_digits=24, decimal_places=2)
    case_reference: str = Field(min_length=3, max_length=200)
    reason: str = Field(min_length=10, max_length=4000)
    authorization: str = Field(pattern=r"^SEIZE$")


class AssetDispositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disposition: Literal["forfeit", "reinvest", "return"]
    amount: Decimal = Field(gt=0, le=50_000_000_000, max_digits=24, decimal_places=2)
    target_account_id: int | None = Field(default=None, ge=1)
    case_reference: str = Field(min_length=3, max_length=200)
    reason: str = Field(min_length=10, max_length=4000)
    authorization: str = Field(pattern=r"^FORECLOSE$")


class IpoReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected", "needs_changes"]
    note: str = Field(min_length=3, max_length=4000)


class DestructiveResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=10, max_length=4000)
    confirmation: Literal["WIPE FCX EQUITY", "WIPE FCX SHARES"]


class SecurityMarginPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    margin_enabled: bool | None = None
    margin_max_leverage: Decimal | None = Field(default=None, ge=1, le=200)


class IndexCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fund_key: str = Field(min_length=2, max_length=40)
    ticker: str = Field(min_length=2, max_length=12)
    display_name: str = Field(min_length=3, max_length=120)
    description: str = Field(default="FCX market-cap weighted index.", max_length=1000)
    target_size: int = Field(default=8, ge=1, le=50)
    base_nav: Decimal = Field(default=Decimal("100"), ge=1, le=1_000_000)
    management_fee_percent: Decimal = Field(default=Decimal("0"), ge=0, le=10)
    auto_populate: bool = True

    @field_validator("fund_key", "ticker")
    @classmethod
    def normalize_index_identifier(cls, value: str) -> str:
        normalized = value.strip().upper().replace(" ", "_")
        if not normalized or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in normalized):
            raise ValueError("Use only letters, numbers, underscores, or hyphens")
        return normalized


class IndexMembersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    security_ids: list[int] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=3, max_length=1000)


class IndexRebalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fund_ids: list[int] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="Automated market-cap restructure", min_length=3, max_length=1000)


class PromotionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_name: str = Field(min_length=2, max_length=120)
    custom_code: str = Field(default="", max_length=32)
    reward_type: Literal["cash", "stock", "random_bundle"]
    cash_amount: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000_000)
    security_id: int | None = Field(default=None, ge=1)
    share_quantity: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000_000)
    bundle_size: int = Field(default=0)
    max_redemptions: int = Field(default=100, ge=1, le=100_000)
    expiry_days: int = Field(default=30, ge=1, le=365)


class PromotionStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


class PromotionRedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    community_user_id: str = Field(min_length=1, max_length=200)
    account_id: str = Field(min_length=4, max_length=200)
    code: str = Field(min_length=6, max_length=40)


class InvestigatorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=12, max_length=1024)
    confirm_password: str = Field(min_length=12, max_length=1024)


class InvestigatorAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    reason: str = Field(default="", max_length=1000)


@router.get("/health")
def health() -> dict[str, Any]:
    with transaction() as connection:
        one(connection, "SELECT 1 AS ok")
        counts = one(
            connection,
            """SELECT
                (SELECT COUNT(*) FROM fcx_communities) AS communities,
                (SELECT COUNT(*) FROM fcx_settlements WHERE state NOT IN ('SETTLED','FAILED','REVERSED')) AS pending_settlements,
                (SELECT COUNT(*) FROM fcx_investigations WHERE status='open') AS open_investigations""",
        ) or {}
    return {"ok": True, "service": "fcx-control", "database": "fcx", "counts": counts}


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    login_id = payload.email.strip().lower()
    if "@" not in login_id:
        login_id = f"{login_id}@fec.local"
    with transaction() as connection:
        user = one(
            connection,
            "SELECT * FROM fcx_control_admin_users WHERE email=:email AND active=TRUE LIMIT 1",
            {"email": login_id},
        )
    if not user or not verify_password(payload.password, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token, csrf, expires = create_session(int(user["id"]), request)
    response.set_cookie(
        "fcx_session", token, httponly=True, secure=settings.cookie_secure,
        samesite="strict", expires=expires, path="/",
    )
    with transaction() as connection:
        execute(connection, "UPDATE fcx_control_admin_users SET last_login_at=NOW(),updated_at=NOW() WHERE id=:id", {"id": user["id"]})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), action="auth.login", request=request)
    return {"ok": True, "csrf_token": csrf, "user": {"id": user["id"], "email": user["email"], "display_name": user["display_name"], "roles": user["roles_json"]}}


@router.get("/auth/session")
def session(user: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
    csrf = secrets.token_urlsafe(32)
    with transaction() as connection:
        execute(
            connection,
            "UPDATE fcx_control_admin_sessions SET csrf_hash=:csrf WHERE id=:id",
            {"csrf": protected_hash(csrf), "id": user["session_id"]},
        )
    return {
        "ok": True,
        "csrf_token": csrf,
        "user": {key: user[key] for key in ("id", "email", "display_name", "roles")},
    }


@router.post("/auth/logout")
def logout(response: Response, user: dict[str, Any] = Depends(require_csrf)) -> dict[str, Any]:
    with transaction() as connection:
        execute(connection, "UPDATE fcx_control_admin_sessions SET revoked_at=NOW() WHERE id=:id", {"id": user["session_id"]})
    response.delete_cookie("fcx_session", path="/")
    return {"ok": True}


@router.get("/admin/overview")
def overview(_: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES, "fec_investigator"))) -> dict[str, Any]:
    with transaction() as connection:
        communities = all_rows(connection, """SELECT *,
            (connection_enabled AND NOT suspended AND last_seen_at IS NOT NULL AND last_seen_at>=NOW()-INTERVAL '5 minutes') AS connected
            FROM fcx_communities ORDER BY community_name""")
        totals = one(
            connection,
            """SELECT
              (SELECT COUNT(*) FROM fcx_ravenhood_accounts WHERE status='active') AS active_accounts,
              (SELECT COUNT(*) FROM fcx_settlements) AS settlements,
              (SELECT COALESCE(SUM(amount),0) FROM fcx_settlements WHERE state='SETTLED') AS settled_value,
              (SELECT COUNT(*) FROM fcx_investigations WHERE status='open') AS open_cases,
              (SELECT COUNT(*) FROM market_securities WHERE active=1 AND lifecycle_status='active') AS securities,
              (SELECT COALESCE(SUM(price*issued_shares),0) FROM market_securities WHERE active=1 AND lifecycle_status='active') AS market_cap,
              (SELECT COUNT(*) FROM market_orders WHERE created_at::timestamptz>=NOW()-INTERVAL '1 hour') AS trades_last_hour,
              (SELECT COALESCE(SUM(gross_amount),0) FROM market_orders WHERE created_at::timestamptz>=NOW()-INTERVAL '1 hour') AS volume_last_hour""",
        ) or {}
        recent = all_rows(connection, "SELECT * FROM fcx_audit_log ORDER BY id DESC LIMIT 25")
        settings_rows = all_rows(connection, "SELECT setting_key,setting_value FROM system_settings")
        settings_map = {str(row["setting_key"]): row["setting_value"] for row in settings_rows}
        live_positions = all_rows(connection, """SELECT p.id,p.direction AS side,p.leverage,p.quantity,p.entry_price,p.entry_notional,
            p.liquidation_price,p.opened_at,s.ticker,s.name,s.price AS current_price,r.account_id,r.display_name,
            COALESCE(STRING_AGG(DISTINCT l.community_id, ', '),'') AS communities,
            CASE WHEN p.direction='short' THEN (p.entry_price-s.price)*p.quantity ELSE (s.price-p.entry_price)*p.quantity END AS unrealized_pnl
            FROM market_margin_positions p JOIN market_securities s ON s.id=p.security_id
            JOIN market_accounts a ON a.id=p.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            WHERE p.status='open' GROUP BY p.id,s.id,r.id ORDER BY ABS(CASE WHEN p.direction='short' THEN (p.entry_price-s.price)*p.quantity ELSE (s.price-p.entry_price)*p.quantity END) DESC LIMIT 12""")
        recent_alerts = all_rows(connection, """SELECT 'risk' AS alert_type,flag_type AS title,severity,last_seen_at AS created_at,security_id::text AS target
            FROM fcx_engine_risk_flags WHERE status='open' ORDER BY last_seen_at DESC LIMIT 10""")
        active_companies = all_rows(connection, """SELECT id,ticker,name,price,previous_price,updated_at FROM market_securities
            WHERE active=1 AND lifecycle_status='active' ORDER BY ticker""")
        recent_trades = all_rows(connection, """SELECT o.created_at,o.side,o.quantity,o.unit_price,o.gross_amount,
            s.ticker,r.account_id,r.display_name FROM market_orders o JOIN market_securities s ON s.id=o.security_id
            JOIN market_accounts a ON a.id=o.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            ORDER BY o.created_at DESC LIMIT 12""")
    try:
        engine = engine_market_snapshot().get("engine", {})
    except Exception as exc:
        engine = {"error": type(exc).__name__, "settings": {"enabled": False}}
    return {"ok": True, "totals": totals, "communities": communities, "recent_actions": recent,
        "settings": settings_map, "live_positions": live_positions, "recent_alerts": recent_alerts,
        "active_companies": active_companies, "recent_trades": recent_trades, "engine": engine}


@router.get("/admin/communities")
def communities(_: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES))) -> dict[str, Any]:
    with transaction() as connection:
        rows = all_rows(connection, "SELECT * FROM fcx_communities ORDER BY community_name")
    return {"ok": True, "communities": rows}


@router.post("/admin/communities")
def create_community(payload: CommunityRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("fcx_admin", "commissioner"))) -> dict[str, Any]:
    now = utcnow()
    with transaction() as connection:
        if one(connection, "SELECT id FROM fcx_communities WHERE community_id=:id", {"id": payload.community_id}):
            raise HTTPException(status_code=409, detail="Community already exists")
        execute(
            connection,
            """INSERT INTO fcx_communities
               (community_id,community_name,bank_bridge_url,bank_secret_env,created_at,updated_at)
               VALUES (:id,:name,:url,:secret,:now,:now)""",
            {"id": payload.community_id, "name": payload.community_name, "url": payload.bank_bridge_url.rstrip("/"), "secret": payload.bank_secret_env, "now": now},
        )
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="community.created", target_type="community", target_id=payload.community_id, new=payload.model_dump(), request=request)
        row = one(connection, "SELECT * FROM fcx_communities WHERE community_id=:id", {"id": payload.community_id})
    return {"ok": True, "community": row}


@router.patch("/admin/communities/{community_id}")
def patch_community(community_id: str, payload: CommunityPatch, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("fcx_admin", "commissioner"))) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No community changes supplied")
    allowed = set(CommunityPatch.model_fields)
    assignments = ",".join(f"{key}=:{key}" for key in changes if key in allowed)
    with transaction() as connection:
        previous = one(connection, "SELECT * FROM fcx_communities WHERE community_id=:id", {"id": community_id})
        if not previous:
            raise HTTPException(status_code=404, detail="Community not found")
        changes["id"] = community_id
        execute(connection, f"UPDATE fcx_communities SET {assignments},updated_at=NOW() WHERE community_id=:id", changes)
        current = one(connection, "SELECT * FROM fcx_communities WHERE community_id=:id", {"id": community_id})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="community.updated", target_type="community", target_id=community_id, previous=previous, new=current, reason=str(changes.get("suspension_reason") or ""), request=request)
    return {"ok": True, "community": current}


@router.post("/admin/communities/{community_id}/credentials")
def create_credential(community_id: str, payload: CredentialRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("fcx_admin", "commissioner"))) -> dict[str, Any]:
    credential_id, plaintext, secret_hash = new_api_credential()
    with transaction() as connection:
        if not one(connection, "SELECT id FROM fcx_communities WHERE community_id=:id", {"id": community_id}):
            raise HTTPException(status_code=404, detail="Community not found")
        execute(
            connection,
            """INSERT INTO fcx_community_credentials
               (community_id,credential_id,secret_hash,scopes_json,created_by,created_at)
               VALUES (:community,:credential,:secret,CAST(:scopes AS jsonb),:user,:now)""",
            {"community": community_id, "credential": credential_id, "secret": secret_hash, "scopes": json.dumps(sorted(set(payload.scopes))), "user": user["id"], "now": utcnow()},
        )
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="credential.generated", target_type="community", target_id=community_id, new={"credential_id": credential_id, "scopes": payload.scopes}, request=request)
    return {"ok": True, "credential_id": credential_id, "credential": plaintext, "notice": "This secret is shown once. Store it in the community Railway service."}


@router.post("/admin/credentials/{credential_id}/revoke")
def revoke_credential(credential_id: str, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("fcx_admin", "commissioner"))) -> dict[str, Any]:
    with transaction() as connection:
        credential = one(connection, "SELECT community_id,active FROM fcx_community_credentials WHERE credential_id=:id", {"id": credential_id})
        if not credential:
            raise HTTPException(status_code=404, detail="Credential not found")
        execute(connection, "UPDATE fcx_community_credentials SET active=FALSE,revoked_at=NOW() WHERE credential_id=:id", {"id": credential_id})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="credential.revoked", target_type="credential", target_id=credential_id, previous=credential, request=request)
    return {"ok": True}


@router.get("/admin/settlements")
def admin_settlements(
    state: str = "",
    community_id: str = "",
    limit: int = 500,
    _: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES, "fec_investigator")),
) -> dict[str, Any]:
    clauses: list[str] = []
    values: dict[str, Any] = {}
    if state:
        clauses.append("s.state=:state")
        values["state"] = state.upper()
    if community_id:
        clauses.append("s.community_id=:community")
        values["community"] = community_id
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    values["limit"] = max(1, min(limit, 2000))
    with transaction() as connection:
        rows = all_rows(connection, f"""SELECT s.*,r.display_name,
            COALESCE((SELECT STRING_AGG(DISTINCT l.community_id, ', ') FROM fcx_ravenhood_links l WHERE l.account_id=r.account_id AND l.active=TRUE),'') AS communities
            FROM fcx_settlements s LEFT JOIN fcx_ravenhood_accounts r ON r.account_id=s.account_id{where.replace(' WHERE ', ' WHERE ' if where else '')}
            ORDER BY s.id DESC LIMIT :limit""", values)
        counts = all_rows(connection, "SELECT state,COUNT(*) AS count FROM fcx_settlements GROUP BY state")
    return {
        "ok": True,
        "settlements": rows,
        "counts": {str(row["state"]).lower(): int(row["count"]) for row in counts},
    }


@router.get("/admin/audit")
def audit_log(limit: int = 200, _: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES, "fec_investigator"))) -> dict[str, Any]:
    limit = max(1, min(1000, limit))
    with transaction() as connection:
        rows = all_rows(connection, "SELECT * FROM fcx_audit_log ORDER BY id DESC LIMIT :limit", {"limit": limit})
    return {"ok": True, "audit": rows}


@router.get("/admin/accounts")
def accounts(limit: int = 200, _: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES, "fec_investigator"))) -> dict[str, Any]:
    safe_limit = min(max(limit, 1), 1000)
    with transaction() as connection:
        rows = all_rows(
            connection,
            """SELECT r.id,r.account_id,r.display_name,r.status,r.created_at,r.updated_at,
                      a.id AS market_account_id,a.cash_balance,a.status AS trading_status,
                      COUNT(DISTINCT l.community_id) AS community_count,
                      COALESCE(STRING_AGG(DISTINCT l.community_id, ', '),'') AS communities,
                      COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id,
                      COALESCE(SUM(h.quantity*s.price),0) AS holdings_value
               FROM fcx_ravenhood_accounts r
               LEFT JOIN market_accounts a ON a.user_id=r.id
               LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
               LEFT JOIN market_holdings h ON h.account_id=a.id
               LEFT JOIN market_securities s ON s.id=h.security_id
               GROUP BY r.id,a.id ORDER BY r.updated_at DESC LIMIT :limit""",
            {"limit": safe_limit},
        )
    return {"ok": True, "accounts": rows}


@router.get("/admin/live-positions")
def live_positions(community: str = "", ticker: str = "", account: str = "", side: str = "", outcome: str = "", limit: int = 500, _: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES, "fec_investigator"))) -> dict[str, Any]:
    clauses = ["p.status='open'"]
    values: dict[str, Any] = {"limit": max(1, min(limit, 1000))}
    if community: clauses.append("EXISTS(SELECT 1 FROM fcx_ravenhood_links fl WHERE fl.account_id=r.account_id AND fl.community_id=:community AND fl.active=TRUE)"); values["community"] = community
    if ticker: clauses.append("UPPER(s.ticker)=UPPER(:ticker)"); values["ticker"] = ticker
    if account: clauses.append("(r.account_id ILIKE :account OR r.display_name ILIKE :account)"); values["account"] = f"%{account}%"
    if side in {"long", "short"}: clauses.append("p.direction=:side"); values["side"] = side
    pnl_sql = "CASE WHEN p.direction='short' THEN (p.entry_price-s.price)*p.quantity ELSE (s.price-p.entry_price)*p.quantity END"
    if outcome == "profitable": clauses.append(f"({pnl_sql})>=0")
    if outcome == "losing": clauses.append(f"({pnl_sql})<0")
    with transaction() as connection:
        rows = all_rows(connection, f"""SELECT p.id,p.account_id AS market_account_id,p.direction AS side,p.leverage,p.quantity,p.entry_price,
            p.entry_notional,p.liquidation_price,p.opened_at,s.ticker,s.name,s.price AS current_price,r.account_id,r.display_name,
            COALESCE(STRING_AGG(DISTINCT l.community_id, ', '),'') AS communities,{pnl_sql} AS unrealized_pnl,
            p.quantity*s.price AS current_value,EXTRACT(EPOCH FROM (NOW()-p.opened_at::timestamptz)) AS age_seconds
            FROM market_margin_positions p JOIN market_securities s ON s.id=p.security_id
            JOIN market_accounts a ON a.id=p.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            WHERE {' AND '.join(clauses)} GROUP BY p.id,s.id,r.id ORDER BY p.opened_at DESC LIMIT :limit""", values)
    return {"ok": True, "positions": rows, "filters": {"community": community, "ticker": ticker, "account": account, "side": side, "outcome": outcome}}


@router.get("/admin/accounts/{account_id}/profile")
def account_profile(account_id: str, _: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES, "fec_investigator"))) -> dict[str, Any]:
    with transaction() as connection:
        account = one(connection, """SELECT r.*,a.id AS market_account_id,a.cash_balance,a.status AS trading_status,
            COALESCE(STRING_AGG(DISTINCT l.community_id, ', '),'') AS communities,
            COALESCE(MAX(l.updated_at),r.updated_at) AS last_activity FROM fcx_ravenhood_accounts r
            LEFT JOIN market_accounts a ON a.user_id=r.id LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            WHERE r.account_id=:account GROUP BY r.id,a.id""", {"account": account_id})
        if not account: raise HTTPException(status_code=404, detail="Ravenhood account not found")
        holdings = all_rows(connection, """SELECT h.*,s.ticker,s.name,s.price,h.quantity*s.price AS current_value,
            (s.price-h.average_cost)*h.quantity AS unrealized_pnl FROM market_holdings h JOIN market_securities s ON s.id=h.security_id
            WHERE h.account_id=:id AND h.quantity<>0 ORDER BY current_value DESC""", {"id": account["market_account_id"]}) if account.get("market_account_id") else []
        leverage = all_rows(connection, """SELECT p.*,s.ticker,s.name,s.price AS current_price,
            CASE WHEN p.direction='short' THEN (p.entry_price-s.price)*p.quantity ELSE (s.price-p.entry_price)*p.quantity END AS unrealized_pnl
            FROM market_margin_positions p JOIN market_securities s ON s.id=p.security_id WHERE p.account_id=:id AND p.status='open' ORDER BY p.opened_at DESC""", {"id": account["market_account_id"]}) if account.get("market_account_id") else []
        stats = one(connection, """SELECT COUNT(*) FILTER(WHERE side='buy') AS total_buys,COUNT(*) FILTER(WHERE side='sell') AS total_sells,
            COALESCE(SUM(CASE WHEN side='sell' THEN gross_amount ELSE 0 END),0) AS sell_volume,
            COALESCE(SUM(CASE WHEN side='buy' THEN gross_amount ELSE 0 END),0) AS buy_volume FROM market_orders WHERE account_id=:id""", {"id": account["market_account_id"]}) if account.get("market_account_id") else {}
        margin_stats = one(connection, """SELECT COUNT(*) FILTER(WHERE status='open') AS open_positions,COUNT(*) FILTER(WHERE status<>'open') AS closed_positions,
            COALESCE(SUM(realized_pnl) FILTER(WHERE status<>'open'),0) AS realized_pnl FROM market_margin_positions WHERE account_id=:id""", {"id": account["market_account_id"]}) if account.get("market_account_id") else {}
        recent = all_rows(connection, """SELECT o.created_at,s.ticker,o.side,o.quantity,o.unit_price,o.gross_amount,o.fee_amount
            FROM market_orders o JOIN market_securities s ON s.id=o.security_id WHERE o.account_id=:id ORDER BY o.created_at DESC LIMIT 20""", {"id": account["market_account_id"]}) if account.get("market_account_id") else []
    return {"ok": True, "account": account, "holdings": holdings, "active_leverage": leverage, "stats": {**(stats or {}), **(margin_stats or {})}, "recent_activity": recent}


@router.get("/admin/fec/promotions")
def promotional_campaigns(_: dict[str, Any] = Depends(require_roles("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        campaigns = all_rows(connection, """SELECT p.*,s.ticker,s.name AS security_name
            FROM market_promo_codes p LEFT JOIN market_securities s ON s.id=p.security_id
            ORDER BY p.created_at DESC LIMIT 500""")
        redemptions = all_rows(connection, """SELECT r.*,p.campaign_name,p.code_hint,
            a.id AS market_account_id,ra.account_id AS ravenhood_account_id,ra.display_name
            FROM market_promo_redemptions r JOIN market_promo_codes p ON p.id=r.promo_id
            JOIN market_accounts a ON a.id=r.account_id JOIN fcx_ravenhood_accounts ra ON ra.id=a.user_id
            ORDER BY r.redeemed_at DESC LIMIT 200""")
        securities = all_rows(connection, """SELECT id,ticker,name FROM market_securities
            WHERE active=1 AND lifecycle_status='active' ORDER BY ticker""")
    return {"ok": True, "campaigns": campaigns, "redemptions": redemptions, "securities": securities}


@router.post("/admin/fec/promotions", status_code=201)
def create_promotional_campaign(payload: PromotionCreateRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    if payload.reward_type == "cash" and payload.cash_amount <= 0:
        raise HTTPException(status_code=400, detail="Cash campaigns require a positive buying-power reward")
    if payload.reward_type == "stock" and (not payload.security_id or payload.share_quantity <= 0):
        raise HTTPException(status_code=400, detail="Stock campaigns require an active security and positive share quantity")
    if payload.reward_type == "random_bundle" and (payload.bundle_size not in {3, 5, 9} or payload.share_quantity <= 0):
        raise HTTPException(status_code=400, detail="Starter portfolios require 3, 5, or 9 stocks and positive shares")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw_code = payload.custom_code.strip().upper().replace(" ", "")
    if not raw_code:
        raw_code = "FCX-" + "".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet) for _ in range(4))
    canonical = "".join(character for character in raw_code if character.isalnum())
    if not 6 <= len(canonical) <= 32:
        raise HTTPException(status_code=400, detail="Promotional codes must contain 6 to 32 letters or numbers")
    now = utcnow()
    expires = now + timedelta(days=payload.expiry_days)
    with transaction() as connection:
        security = None
        if payload.security_id:
            security = one(connection, "SELECT id,ticker FROM market_securities WHERE id=:id AND active=1 AND lifecycle_status='active'", {"id": payload.security_id})
            if not security:
                raise HTTPException(status_code=400, detail="Select an active FCX security")
        duplicate = one(connection, "SELECT id FROM market_promo_codes WHERE code_hash=:hash", {"hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()})
        if duplicate:
            raise HTTPException(status_code=409, detail="That promotional code already exists")
        campaign = one(connection, """INSERT INTO market_promo_codes
            (campaign_name,code_hash,code_hint,code_plain,reward_type,cash_amount,security_id,share_quantity,
             bundle_size,max_redemptions,expires_at,active,created_by,created_by_name,created_at)
            VALUES (:name,:hash,:hint,:code,:reward,:cash,:security,:shares,:bundle,:maximum,:expires,1,:actor,:actor_name,:created)
            RETURNING id""", {"name": payload.campaign_name.strip(), "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "hint": canonical[-4:], "code": raw_code, "reward": payload.reward_type, "cash": payload.cash_amount,
            "security": security["id"] if security else None, "shares": payload.share_quantity, "bundle": payload.bundle_size,
            "maximum": payload.max_redemptions, "expires": expires.isoformat(), "actor": user["id"],
            "actor_name": user["display_name"], "created": now.isoformat()})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]),
            action="fec.promotion.created", target_type="promotional_campaign", target_id=str(campaign["id"]),
            new={**payload.model_dump(), "custom_code": "", "code_hint": canonical[-4:]}, request=request)
    return {"ok": True, "campaign_id": campaign["id"], "campaign_name": payload.campaign_name, "code": raw_code, "expires_at": expires.isoformat()}


@router.patch("/admin/fec/promotions/{campaign_id}")
def update_promotional_campaign(campaign_id: int, payload: PromotionStatusRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        existing = one(connection, "SELECT * FROM market_promo_codes WHERE id=:id", {"id": campaign_id})
        if not existing:
            raise HTTPException(status_code=404, detail="Promotional campaign not found")
        execute(connection, "UPDATE market_promo_codes SET active=:active WHERE id=:id", {"active": int(payload.active), "id": campaign_id})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]),
            action="fec.promotion.resumed" if payload.active else "fec.promotion.paused", target_type="promotional_campaign",
            target_id=str(campaign_id), previous=existing, new=payload.model_dump(), request=request)
    return {"ok": True, "active": payload.active}


@router.delete("/admin/fec/promotions/{campaign_id}")
def delete_promotional_campaign(campaign_id: int, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        existing = one(connection, "SELECT * FROM market_promo_codes WHERE id=:id", {"id": campaign_id})
        if not existing:
            raise HTTPException(status_code=404, detail="Promotional campaign not found")
        if int(existing.get("redemption_count") or 0) > 0:
            raise HTTPException(status_code=409, detail="Redeemed campaigns must be paused and retained for the FEC audit record")
        execute(connection, "DELETE FROM market_promo_codes WHERE id=:id", {"id": campaign_id})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]),
            action="fec.promotion.deleted", target_type="promotional_campaign", target_id=str(campaign_id),
            previous=existing, request=request)
    return {"ok": True}


@router.post("/community/promotions/redeem")
def redeem_promotional_campaign(payload: PromotionRedeemRequest, principal: dict[str, Any] = Depends(require_community_scopes("trade:write"))) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    canonical = "".join(character for character in payload.code.upper() if character.isalnum())
    code_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    now = utcnow().isoformat()
    with transaction() as connection:
        account = one(connection, """SELECT a.id,a.status,r.account_id,r.display_name
            FROM fcx_ravenhood_links l JOIN fcx_ravenhood_accounts r ON r.account_id=l.account_id
            JOIN market_accounts a ON a.user_id=r.id
            WHERE l.community_id=:community AND l.community_user_id=:user AND l.account_id=:account AND l.active=TRUE
            FOR UPDATE OF a""", {"community": community_id, "user": payload.community_user_id, "account": payload.account_id})
        if not account:
            raise HTTPException(status_code=404, detail="Ravenhood account link not found for this community")
        campaign = one(connection, """SELECT p.*,s.ticker FROM market_promo_codes p
            LEFT JOIN market_securities s ON s.id=p.security_id WHERE p.code_hash=:hash FOR UPDATE OF p""", {"hash": code_hash})
        if not campaign or not bool(campaign["active"]) or (campaign.get("expires_at") and str(campaign["expires_at"]) <= now):
            raise HTTPException(status_code=404, detail="This promotional code is invalid or no longer active")
        if int(campaign["redemption_count"] or 0) >= int(campaign["max_redemptions"] or 0):
            raise HTTPException(status_code=409, detail="This promotional campaign has reached its redemption limit")
        if one(connection, "SELECT id FROM market_promo_redemptions WHERE promo_id=:promo AND account_id=:account", {"promo": campaign["id"], "account": account["id"]}):
            raise HTTPException(status_code=409, detail="This account has already redeemed the campaign")
        reward_type = str(campaign["reward_type"])
        if reward_type == "cash":
            amount = Decimal(str(campaign["cash_amount"] or 0))
            execute(connection, "UPDATE market_accounts SET cash_balance=cash_balance+:amount,updated_at=:now WHERE id=:id", {"amount": amount, "now": now, "id": account["id"]})
            summary = f"${amount:,.2f} Ravenhood buying power"
        else:
            if reward_type == "stock":
                securities = all_rows(connection, """SELECT s.id,s.ticker FROM market_securities s
                    WHERE s.id=:id AND s.active=1 AND s.lifecycle_status='active'
                    AND NOT EXISTS (SELECT 1 FROM market_security_halts h WHERE h.security_id=s.id AND h.status='active')""", {"id": campaign["security_id"]})
            else:
                securities = all_rows(connection, """SELECT s.id,s.ticker FROM market_securities s
                    WHERE s.active=1 AND s.lifecycle_status='active' AND s.security_type='stock'
                    AND NOT EXISTS (SELECT 1 FROM market_security_halts h WHERE h.security_id=s.id AND h.status='active')
                    ORDER BY RANDOM() LIMIT :limit""", {"limit": int(campaign["bundle_size"] or 0)})
            if not securities:
                raise HTTPException(status_code=409, detail="The securities assigned to this campaign are unavailable")
            quantity = Decimal(str(campaign["share_quantity"] or 0))
            for security in securities:
                execute(connection, """INSERT INTO market_holdings (account_id,security_id,quantity,average_cost)
                    VALUES (:account,:security,:quantity,0) ON CONFLICT (account_id,security_id)
                    DO UPDATE SET quantity=market_holdings.quantity+EXCLUDED.quantity""", {"account": account["id"], "security": security["id"], "quantity": quantity})
            summary = ", ".join(f"{quantity:g} {security['ticker']}" for security in securities)
        execute(connection, """INSERT INTO market_promo_redemptions (promo_id,account_id,community_id,reward_summary,redeemed_at)
            VALUES (:promo,:account,:community,:summary,:now)""", {"promo": campaign["id"], "account": account["id"], "community": community_id, "summary": summary, "now": now})
        execute(connection, "UPDATE market_promo_codes SET redemption_count=redemption_count+1 WHERE id=:id", {"id": campaign["id"]})
        audit(connection, actor_type="community", actor_id=community_id, actor_role="trade:write", action="promotion.redeemed",
            target_type="ravenhood_account", target_id=payload.account_id, new={"campaign_id": campaign["id"], "reward": summary})
    return {"ok": True, "campaign_name": campaign["campaign_name"], "reward_summary": summary}


@router.get("/admin/investigations")
def investigations(limit: int = 200, _: dict[str, Any] = Depends(require_roles("developer", "fec_admin"))) -> dict[str, Any]:
    safe_limit = min(max(limit, 1), 1000)
    with transaction() as connection:
        rows = all_rows(
            connection,
            """SELECT i.*,r.display_name,
                      COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id
               FROM fcx_investigations i JOIN fcx_ravenhood_accounts r ON r.account_id=i.account_id
               LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
               GROUP BY i.id,r.display_name
               ORDER BY CASE i.priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2 ELSE 3 END,
                        i.updated_at DESC LIMIT :limit""",
            {"limit": safe_limit},
        )
    return {"ok": True, "investigations": rows}


@router.get("/admin/investigations/accounts/{account_id}/history")
def investigation_account_history(account_id: str, page: int = 1, page_size: int = 50, search: str = "", symbol: str = "", transaction_type: str = "", sort: Literal["asc", "desc"] = "desc", _: dict[str, Any] = Depends(require_roles("developer", "fec_admin", "fec_investigator"))) -> dict[str, Any]:
    page = max(1, page); page_size = max(10, min(page_size, 100)); offset = (page - 1) * page_size
    with transaction() as connection:
        account = one(connection, """SELECT r.account_id,r.display_name,r.status,a.id AS market_account_id,a.cash_balance,
            COALESCE(STRING_AGG(DISTINCT l.community_id, ', '),'') AS communities FROM fcx_ravenhood_accounts r
            LEFT JOIN market_accounts a ON a.user_id=r.id LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            WHERE r.account_id=:account GROUP BY r.id,a.id""", {"account": account_id})
        if not account or not account.get("market_account_id"): raise HTTPException(status_code=404, detail="Ravenhood trading account not found")
        values = {"id": account["market_account_id"], "search": f"%{search}%", "symbol": symbol.upper(), "type": transaction_type.lower(), "limit": page_size, "offset": offset}
        history_sql = """SELECT * FROM (
            SELECT o.id,'equity_'||LOWER(o.side) AS transaction_type,o.created_at AS occurred_at,s.ticker,s.name,o.quantity,o.unit_price AS price,o.gross_amount AS value,NULL::numeric AS leverage,NULL::numeric AS realized_pnl,o.side AS direction
            FROM market_orders o JOIN market_securities s ON s.id=o.security_id WHERE o.account_id=:id
            UNION ALL
            SELECT p.id,CASE WHEN p.status='open' THEN 'leverage_open' WHEN p.close_reason ILIKE '%liquidat%' THEN 'liquidation' ELSE 'leverage_close' END,
                CASE WHEN p.status='open' THEN p.opened_at ELSE COALESCE(p.closed_at,p.opened_at) END,s.ticker,s.name,p.quantity,
                CASE WHEN p.status='open' THEN p.entry_price ELSE COALESCE(p.close_price,p.entry_price) END,
                CASE WHEN p.status='open' THEN p.entry_notional ELSE COALESCE(p.close_notional,p.entry_notional) END,p.leverage,p.realized_pnl,p.direction
            FROM market_margin_positions p JOIN market_securities s ON s.id=p.security_id WHERE p.account_id=:id
        ) history WHERE (:search='' OR ticker ILIKE :search OR name ILIKE :search OR transaction_type ILIKE :search)
          AND (:symbol='' OR UPPER(ticker)=:symbol) AND (:type='' OR LOWER(transaction_type)=:type)"""
        total = one(connection, f"SELECT COUNT(*) AS count FROM ({history_sql}) counted", values) or {"count": 0}
        rows = all_rows(connection, f"{history_sql} ORDER BY occurred_at {sort.upper()},id {sort.upper()} LIMIT :limit OFFSET :offset", values)
        leverage = all_rows(connection, """SELECT p.*,s.ticker,s.name,s.price AS current_price,
            CASE WHEN p.direction='short' THEN (p.entry_price-s.price)*p.quantity ELSE (s.price-p.entry_price)*p.quantity END AS unrealized_pnl
            FROM market_margin_positions p JOIN market_securities s ON s.id=p.security_id WHERE p.account_id=:id AND p.status='open' ORDER BY p.opened_at DESC""", {"id": account["market_account_id"]})
    return {"ok": True, "account": account, "active_leverage": leverage, "history": rows, "pagination": {"page": page, "page_size": page_size, "total": int(total["count"] or 0), "pages": max(1, (int(total["count"] or 0)+page_size-1)//page_size)}}


@router.get("/admin/investigations/accounts/{account_id}/analytics")
def investigation_account_analytics(account_id: str, period: Literal["day", "week", "month", "year"] = "day", _: dict[str, Any] = Depends(require_roles("developer", "fec_admin", "fec_investigator"))) -> dict[str, Any]:
    interval = {"day": "1 day", "week": "7 days", "month": "30 days", "year": "365 days"}[period]
    with transaction() as connection:
        account = one(connection, """SELECT a.id FROM market_accounts a JOIN fcx_ravenhood_accounts r ON r.id=a.user_id WHERE r.account_id=:account""", {"account": account_id})
        if not account: raise HTTPException(status_code=404, detail="Ravenhood trading account not found")
        equity = one(connection, """SELECT COUNT(*) AS trades,COUNT(*) FILTER(WHERE side='buy') AS buys,COUNT(*) FILTER(WHERE side='sell') AS sells,
            COALESCE(SUM(gross_amount) FILTER(WHERE side='buy'),0) AS buy_volume,COALESCE(SUM(gross_amount) FILTER(WHERE side='sell'),0) AS sell_volume
            FROM market_orders WHERE account_id=:id AND created_at::timestamptz>=NOW()-CAST(:period AS interval)""", {"id": account["id"], "period": interval}) or {}
        margin = one(connection, """SELECT COALESCE(SUM(realized_pnl) FILTER(WHERE status<>'open' AND closed_at::timestamptz>=NOW()-CAST(:period AS interval)),0) AS realized_pnl,
            COUNT(*) FILTER(WHERE realized_pnl>0 AND status<>'open' AND closed_at::timestamptz>=NOW()-CAST(:period AS interval)) AS winning_trades,
            COUNT(*) FILTER(WHERE realized_pnl<0 AND status<>'open' AND closed_at::timestamptz>=NOW()-CAST(:period AS interval)) AS losing_trades,
            COALESCE(SUM(CASE WHEN status='open' AND direction='short' THEN (entry_price-s.price)*quantity WHEN status='open' THEN (s.price-entry_price)*quantity ELSE 0 END),0) AS unrealized_pnl
            FROM market_margin_positions p JOIN market_securities s ON s.id=p.security_id WHERE p.account_id=:id""", {"id": account["id"], "period": interval}) or {}
    realized = Decimal(str(margin.get("realized_pnl") or 0)); unrealized = Decimal(str(margin.get("unrealized_pnl") or 0))
    return {"ok": True, "period": period, "analytics": {**equity, **margin, "combined_pnl": realized+unrealized}}


@router.get("/admin/system-health")
def system_health(_: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES, "fec_investigator"))) -> dict[str, Any]:
    checked = utcnow()
    with transaction() as connection:
        one(connection, "SELECT 1 AS ok")
        communities = all_rows(connection, """SELECT *,
            (connection_enabled AND NOT suspended AND last_seen_at IS NOT NULL AND last_seen_at>=NOW()-INTERVAL '5 minutes') AS connected
            FROM fcx_communities ORDER BY community_name""")
        counts = one(connection, """SELECT (SELECT COUNT(*) FROM fcx_settlements WHERE state IN ('CREATED','BANK_AUTHORIZED','FAILED')) AS banking_backlog,
            (SELECT COUNT(*) FROM market_margin_positions WHERE status='open') AS open_leverage,
            (SELECT COUNT(*) FROM fcx_ravenhood_accounts WHERE status='active') AS active_accounts,
            (SELECT COUNT(*) FROM fcx_control_admin_sessions WHERE revoked_at IS NULL AND expires_at>NOW()) AS active_sessions""") or {}
        state = one(connection, "SELECT status,last_heartbeat_at,last_error FROM fcx_engine_state WHERE id=1") or {}
        settings_rows = all_rows(connection, "SELECT setting_key,setting_value FROM system_settings")
    settings_map = {row["setting_key"]: row["setting_value"] for row in settings_rows}
    community_health = []
    for row in communities:
        last_seen = row.get("last_seen_at")
        connected = bool(row.get("connection_enabled") and not row.get("suspended") and last_seen and (checked-last_seen).total_seconds()<300)
        community_health.append({**row, "connected": connected, "health": "healthy" if connected else "error" if row.get("connection_enabled") else "warning"})
    services = [
        {"name": "FCX API", "status": "healthy", "detail": "Authenticated API responding"},
        {"name": "Database", "status": "healthy", "detail": "Primary FCX query succeeded"},
        {"name": "FCX Engine", "status": "healthy" if str(state.get("status","")).lower() not in {"error","failed","stopped"} else "error", "detail": state.get("last_error") or state.get("status") or "Ready"},
        {"name": "Market Engine", "status": "healthy" if str(settings_map.get("market_open","1")) not in {"0","false"} else "warning", "detail": "Market open" if str(settings_map.get("market_open","1")) not in {"0","false"} else "Administratively halted"},
        {"name": "Trading Engine", "status": "healthy" if str(settings_map.get("trading_enabled","1")) not in {"0","false"} else "warning", "detail": "Trading enabled" if str(settings_map.get("trading_enabled","1")) not in {"0","false"} else "Trading disabled"},
        {"name": "Banking Services", "status": "warning" if int(counts.get("banking_backlog") or 0)>25 else "healthy", "detail": f"{int(counts.get('banking_backlog') or 0)} recoverable/pending"},
        {"name": "Leverage Engine", "status": "healthy" if str(settings_map.get("market_margin_enabled","1")) not in {"0","false"} else "warning", "detail": f"{int(counts.get('open_leverage') or 0)} open positions"},
        {"name": "Account Services", "status": "healthy", "detail": f"{int(counts.get('active_accounts') or 0)} active Ravenhood accounts"},
        {"name": "Authentication", "status": "healthy", "detail": f"{int(counts.get('active_sessions') or 0)} active sessions"},
    ]
    return {"ok": True, "checked_at": checked, "services": services, "communities": community_health, "engine": state}


@router.get("/admin/fec/investigators")
def list_investigators(_: dict[str, Any] = Depends(require_roles("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        rows = all_rows(connection, """SELECT u.id,u.email,u.display_name,u.roles_json,u.active,u.last_login_at,u.created_at,u.deleted_at,u.access_note,
            creator.display_name AS created_by_name FROM fcx_control_admin_users u LEFT JOIN fcx_control_admin_users creator ON creator.id=u.created_by
            WHERE u.roles_json ? 'fec_investigator' ORDER BY u.created_at DESC""")
    return {"ok": True, "investigators": rows}


@router.post("/admin/fec/investigators", status_code=201)
def create_investigator(payload: InvestigatorCreateRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    if payload.password != payload.confirm_password: raise HTTPException(status_code=400, detail="Passwords do not match")
    username = payload.username.strip(); email = username.lower() if "@" in username else f"{username.lower()}@fec.local"
    with transaction() as connection:
        if one(connection, "SELECT id FROM fcx_control_admin_users WHERE email=:email", {"email": email}): raise HTTPException(status_code=409, detail="Investigator username already exists")
        created = one(connection, """INSERT INTO fcx_control_admin_users (email,display_name,password_hash,roles_json,active,created_by,created_at,updated_at)
            VALUES (:email,:name,:password,'[\"fec_investigator\"]'::jsonb,TRUE,:actor,NOW(),NOW()) RETURNING id,email,display_name,active,created_at""",
            {"email": email, "name": username, "password": hash_password(payload.password), "actor": user["id"]})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.investigator.created", target_type="admin_user", target_id=str(created["id"]), new={"username": username, "roles": ["fec_investigator"]}, request=request)
    return {"ok": True, "investigator": created}


@router.patch("/admin/fec/investigators/{investigator_id}")
def update_investigator_access(investigator_id: int, payload: InvestigatorAccessRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        existing = one(connection, "SELECT id,email,display_name,active,deleted_at FROM fcx_control_admin_users WHERE id=:id AND roles_json ? 'fec_investigator'", {"id": investigator_id})
        if not existing: raise HTTPException(status_code=404, detail="FEC investigator not found")
        if existing.get("deleted_at") and payload.active: raise HTTPException(status_code=409, detail="Deleted investigator accounts cannot be restored")
        execute(connection, "UPDATE fcx_control_admin_users SET active=:active,access_note=:reason,updated_at=NOW() WHERE id=:id", {"active": payload.active, "reason": payload.reason, "id": investigator_id})
        if not payload.active: execute(connection, "UPDATE fcx_control_admin_sessions SET revoked_at=NOW() WHERE user_id=:id AND revoked_at IS NULL", {"id": investigator_id})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.investigator.restored" if payload.active else "fec.investigator.revoked", target_type="admin_user", target_id=str(investigator_id), previous=existing, new=payload.model_dump(), reason=payload.reason, request=request)
    return {"ok": True, "active": payload.active}


@router.delete("/admin/fec/investigators/{investigator_id}")
def delete_investigator(investigator_id: int, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        existing = one(connection, "SELECT id,email,display_name,active FROM fcx_control_admin_users WHERE id=:id AND roles_json ? 'fec_investigator'", {"id": investigator_id})
        if not existing: raise HTTPException(status_code=404, detail="FEC investigator not found")
        execute(connection, "UPDATE fcx_control_admin_users SET active=FALSE,deleted_at=NOW(),email='deleted+'||id||'@fec.invalid',updated_at=NOW() WHERE id=:id", {"id": investigator_id})
        execute(connection, "UPDATE fcx_control_admin_sessions SET revoked_at=NOW() WHERE user_id=:id AND revoked_at IS NULL", {"id": investigator_id})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.investigator.deleted", target_type="admin_user", target_id=str(investigator_id), previous=existing, new={"soft_deleted": True}, request=request)
    return {"ok": True}


@router.patch("/admin/investigations/{case_id}")
def update_investigation(case_id: int, payload: InvestigationPatch, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No investigation changes supplied")
    assignments: list[str] = []
    parameters: dict[str, Any] = {"id": case_id}
    for key, value in changes.items():
        if key == "notes_json":
            assignments.append("notes_json=CAST(:notes_json AS jsonb)")
            parameters[key] = json.dumps(value)
        else:
            assignments.append(f"{key}=:{key}")
            parameters[key] = value
    with transaction() as connection:
        existing = one(connection, "SELECT * FROM fcx_investigations WHERE id=:id", {"id": case_id})
        if not existing:
            raise HTTPException(status_code=404, detail="Investigation not found")
        execute(connection, f"UPDATE fcx_investigations SET {','.join(assignments)},updated_at=NOW() WHERE id=:id", parameters)
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="investigation.updated", target_type="investigation", target_id=str(case_id), previous=existing, new=changes, request=request)
    return {"ok": True}


@router.get("/admin/market")
def market_admin(_: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES))) -> dict[str, Any]:
    with transaction() as connection:
        securities = all_rows(connection, "SELECT * FROM market_securities WHERE active=1 AND lifecycle_status='active' ORDER BY ticker")
        settings_rows = all_rows(connection, "SELECT setting_key,setting_value,updated_at FROM system_settings ORDER BY setting_key")
        order_counts = all_rows(connection, "SELECT status,COUNT(*) AS count FROM market_order_requests GROUP BY status ORDER BY status")
        recent_orders = all_rows(connection, """SELECT o.*,s.ticker,r.display_name
            FROM market_orders o JOIN market_securities s ON s.id=o.security_id
            JOIN market_accounts a ON a.id=o.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            ORDER BY o.id DESC LIMIT 100""")
    return {"ok": True, "securities": securities, "settings": settings_rows, "order_counts": order_counts, "recent_orders": recent_orders}


@router.patch("/admin/market/settings")
def update_market_settings(payload: MarketSettingsPatch, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No market settings supplied")
    key_map = {
        "manual_override": "market_manual_override",
        "schedule_open_time": "market_schedule_open_time",
        "schedule_close_time": "market_schedule_close_time",
        "schedule_timezone": "market_schedule_timezone",
        "weekends_enabled": "market_weekends_enabled",
        "fcxv_24h_enabled": "market_fcxv_24h_enabled",
        "transfer_fee_percent": "market_transfer_fee_percent",
        "trade_fee_percent": "market_trade_fee_percent",
        "margin_enabled": "market_margin_enabled",
        "margin_maintenance_percent": "market_margin_maintenance_percent",
        "margin_max_open_positions": "market_margin_max_open_positions",
        "margin_max_account_notional": "market_margin_max_account_notional",
        "liquidation_hunts_enabled": "market_liquidation_hunts_enabled",
        "liquidation_hunt_threshold": "market_liquidation_hunt_threshold",
        "liquidation_hunt_probability_percent": "market_liquidation_hunt_probability_percent",
        "liquidation_hunt_intensity": "market_liquidation_hunt_intensity",
        "liquidation_hunt_max_move_percent": "market_liquidation_hunt_max_move_percent",
        "liquidation_hunt_cooldown_minutes": "market_liquidation_hunt_cooldown_minutes",
        "liquidation_hunt_market_hours_only": "market_liquidation_hunt_market_hours_only",
    }
    stored_changes = {key_map.get(key, key): value for key, value in changes.items()}
    with transaction() as connection:
        previous_rows = all_rows(connection, "SELECT setting_key,setting_value FROM system_settings")
        previous = {row["setting_key"]: row["setting_value"] for row in previous_rows if row["setting_key"] in stored_changes}
        for key, value in stored_changes.items():
            stored = "1" if value is True else "0" if value is False else str(value)
            execute(connection, """INSERT INTO system_settings(setting_key,setting_value,updated_at) VALUES (:key,:value,NOW())
                ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at""", {"key": key, "value": stored})
        if changes.get("manual_override") in {"open", "closed"}:
            forced_open = changes["manual_override"] == "open"
            execute(connection, """INSERT INTO system_settings(setting_key,setting_value,updated_at) VALUES ('market_open',:value,NOW())
                ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at""", {"value": "1" if forced_open else "0"})
            stored_changes["market_open"] = forced_open
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="market.settings.updated", target_type="market", previous=previous, new=stored_changes, request=request)
    return {"ok": True, "settings": stored_changes}


@router.get("/admin/engine/snapshot")
def engine_snapshot(_: dict[str, Any] = Depends(require_roles("developer", "fec_admin"))) -> dict[str, Any]:
    """Expose the FCX-owned engine only through the authenticated FEC session."""
    return engine_market_snapshot()


@router.patch("/admin/engine/settings")
def engine_settings(
    payload: EngineSettingsRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin")),
) -> dict[str, Any]:
    result = update_engine_configuration(payload)
    with transaction() as connection:
        audit(
            connection,
            actor_type="admin",
            actor_id=str(user["id"]),
            actor_role=",".join(user["roles"]),
            action="engine.settings.updated",
            target_type="fcx_engine",
            new=payload.model_dump(exclude_none=True),
            request=request,
        )
    return result


@router.post("/admin/engine/cycle")
def engine_cycle(
    payload: EngineCycleRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin")),
) -> dict[str, Any]:
    result = run_engine_cycle(payload)
    with transaction() as connection:
        audit(
            connection,
            actor_type="admin",
            actor_id=str(user["id"]),
            actor_role=",".join(user["roles"]),
            action="engine.cycle.manual",
            target_type="fcx_engine",
            target_id=payload.cycle,
            new=result.get("result", {}),
            request=request,
        )
    return result


@router.post("/admin/engine/seed")
def engine_seed(
    payload: EngineSeedRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin")),
) -> dict[str, Any]:
    result = seed_engine_population(payload)
    with transaction() as connection:
        audit(
            connection,
            actor_type="admin",
            actor_id=str(user["id"]),
            actor_role=",".join(user["roles"]),
            action="engine.population.seeded",
            target_type="fcx_engine",
            new=result.get("result", {}),
            request=request,
        )
    return result


@router.post("/admin/engine/pause")
def engine_pause(
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin")),
) -> dict[str, Any]:
    previous = engine_market_snapshot()
    result = pause_engine_market()
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="engine.paused", target_type="fcx_engine", previous={"enabled": previous.get("settings", {}).get("enabled")}, new={"enabled": False}, request=request)
    return result


@router.post("/admin/engine/resume")
def engine_resume(
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin")),
) -> dict[str, Any]:
    previous = engine_market_snapshot()
    result = resume_engine_market()
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="engine.resumed", target_type="fcx_engine", previous={"enabled": previous.get("settings", {}).get("enabled")}, new={"enabled": True}, request=request)
    return result


@router.post("/admin/engine/sandbox")
def engine_sandbox(
    payload: EngineSandboxRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin")),
) -> dict[str, Any]:
    result = run_engine_sandbox(payload)
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="engine.sandbox.completed", target_type="fcx_engine", new={"days": payload.days, "seed": payload.seed}, request=request)
    return result


@router.post("/admin/engine/kill-switch")
def engine_kill_switch(
    payload: EngineKillSwitchRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin")),
) -> dict[str, Any]:
    result = set_engine_kill_switch(payload)
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="engine.kill_switch.updated", target_type="fcx_engine", new={"active": payload.active}, request=request)
    return result


@router.post("/admin/engine/ticker/{ticker}/pause")
def engine_ticker_pause(ticker: str, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    result = pause_engine_ticker(ticker)
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="engine.ticker.paused", target_type="security", target_id=ticker.upper(), request=request)
    return result


@router.post("/admin/engine/ticker/{ticker}/resume")
def engine_ticker_resume(ticker: str, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    result = resume_engine_ticker(ticker)
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="engine.ticker.resumed", target_type="security", target_id=ticker.upper(), request=request)
    return result


@router.post("/admin/engine/corporate-actions/split")
def engine_stock_split(payload: EngineStockSplitRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    result = run_engine_stock_split(payload)
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="engine.stock_split.applied", target_type="security", target_id=payload.ticker.upper(), new=payload.model_dump(), request=request)
    return result


@router.post("/admin/engine/corporate-actions/dividend")
def engine_dividend(payload: EngineDividendRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    result = run_engine_dividend(payload)
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="engine.dividend.declared", target_type="security", target_id=payload.ticker.upper(), new=payload.model_dump(), request=request)
    return result


def _eligible_index_securities(connection: Any) -> list[dict[str, Any]]:
    rows = all_rows(connection, """SELECT id,ticker,name,sector,price,issued_shares,volatility,index_eligible
        FROM market_securities
        WHERE active=1 AND lifecycle_status='active' AND index_eligible=1
          AND security_type<>'fund'
        ORDER BY ticker""")
    for row in rows:
        row["market_cap"] = security_market_cap(row)
    return rows


def _index_admin_payload(connection: Any) -> dict[str, Any]:
    funds = all_rows(connection, """SELECT f.*,s.ticker,s.name,s.description,s.price,s.previous_price,
            s.issued_shares,(s.price*s.issued_shares) AS market_cap
        FROM market_index_funds f JOIN market_securities s ON s.id=f.security_id
        ORDER BY f.enabled DESC,f.fund_key""")
    members = all_rows(connection, """SELECT m.fund_id,m.security_id,m.weight,m.score,m.reference_price,
            m.market_cap_at_rebalance,m.realized_volatility,m.rank,m.added_at,
            s.ticker,s.name,s.sector,s.price,s.issued_shares,
            (s.price*s.issued_shares) AS current_market_cap
        FROM market_index_members m JOIN market_securities s ON s.id=m.security_id
        ORDER BY m.fund_id,m.rank,s.ticker""")
    members_by_fund: dict[int, list[dict[str, Any]]] = {}
    for member in members:
        members_by_fund.setdefault(int(member["fund_id"]), []).append(member)
    for fund in funds:
        fund["members"] = members_by_fund.get(int(fund["id"]), [])
    eligible = _eligible_index_securities(connection)
    return {
        "ok": True,
        "funds": funds,
        "eligible_securities": eligible,
        "eligible_market_cap": sum((security_market_cap(row) for row in eligible), Decimal("0")),
    }


def _replace_index_members(
    connection: Any,
    fund: dict[str, Any],
    securities: list[dict[str, Any]],
    timestamp: str,
    *,
    update_target_size: bool,
) -> list[dict[str, Any]]:
    weights = market_cap_weights(securities)
    execute(connection, "DELETE FROM market_index_members WHERE fund_id=:fund", {"fund": fund["id"]})
    result: list[dict[str, Any]] = []
    for rank, (security, weight) in enumerate(zip(securities, weights), start=1):
        market_cap = security_market_cap(security)
        execute(connection, """INSERT INTO market_index_members
            (fund_id,security_id,weight,score,reference_price,market_cap_at_rebalance,realized_volatility,rank,added_at)
            VALUES (:fund,:security,:weight,:score,:price,:market_cap,:volatility,:rank,:added)""", {
                "fund": fund["id"], "security": security["id"], "weight": weight,
                "score": market_cap, "price": security["price"], "market_cap": market_cap,
                "volatility": security.get("volatility") or 0, "rank": rank, "added": timestamp,
            })
        result.append({"security_id": int(security["id"]), "ticker": security["ticker"], "weight": weight, "market_cap": market_cap, "rank": rank})
    values = {"base_nav": fund.get("price") or fund.get("base_nav") or 100, "now": timestamp, "id": fund["id"], "size": len(securities)}
    target_assignment = ",target_size=:size" if update_target_size else ""
    execute(connection, f"""UPDATE market_index_funds SET base_nav=:base_nav,last_rebalanced_at=:now,
        last_valued_at=:now,updated_at=:now{target_assignment} WHERE id=:id""", values)
    return result


@router.get("/admin/indexes")
def index_settings(_: dict[str, Any] = Depends(require_roles("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        return _index_admin_payload(connection)


@router.post("/admin/indexes")
def create_index(payload: IndexCreateRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    timestamp = utcnow().isoformat()
    with transaction() as connection:
        if one(connection, "SELECT id FROM market_index_funds WHERE fund_key=:key", {"key": payload.fund_key}):
            raise HTTPException(status_code=409, detail="That index key already exists")
        if one(connection, "SELECT id FROM market_securities WHERE ticker=:ticker", {"ticker": payload.ticker}):
            raise HTTPException(status_code=409, detail="That FCX ticker is already assigned")
        security = one(connection, """INSERT INTO market_securities
            (ticker,name,security_type,sector,description,price,previous_price,volatility,active,lifecycle_status,
             issued_shares,index_eligible,margin_enabled,margin_max_leverage,updated_at)
            VALUES (:ticker,:name,'fund','Index',:description,:nav,:nav,0.5,1,'active',1000000,0,0,1,:now)
            RETURNING id,ticker,name,price""", {
                "ticker": payload.ticker, "name": payload.display_name, "description": payload.description,
                "nav": payload.base_nav, "now": timestamp,
            })
        if not security:
            raise HTTPException(status_code=500, detail="FCX index security could not be created")
        fund = one(connection, """INSERT INTO market_index_funds
            (fund_key,security_id,display_name,risk_profile,target_size,base_nav,management_fee_percent,
             enabled,rebalance_interval_hours,created_at,updated_at)
            VALUES (:key,:security,:name,'market_cap',:size,:nav,:fee,1,24,:now,:now)
            RETURNING *""", {
                "key": payload.fund_key, "security": security["id"], "name": payload.display_name,
                "size": payload.target_size, "nav": payload.base_nav,
                "fee": payload.management_fee_percent, "now": timestamp,
            })
        selected: list[dict[str, Any]] = []
        if fund and payload.auto_populate:
            selected = rank_by_market_cap(_eligible_index_securities(connection), payload.target_size)
            _replace_index_members(connection, {**fund, "price": security["price"]}, selected, timestamp, update_target_size=False)
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]),
            action="market.index.created", target_type="market_index", target_id=str(fund["id"] if fund else ""),
            new={**payload.model_dump(), "members": [row["ticker"] for row in selected]}, request=request)
        response = _index_admin_payload(connection)
    return {**response, "created_index_id": int(fund["id"] if fund else 0)}


@router.put("/admin/indexes/{fund_id}/members")
def replace_index_members(fund_id: int, payload: IndexMembersRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    timestamp = utcnow().isoformat()
    security_ids = list(dict.fromkeys(int(value) for value in payload.security_ids))
    if len(security_ids) != len(payload.security_ids):
        raise HTTPException(status_code=400, detail="An index cannot contain the same security twice")
    with transaction() as connection:
        fund = one(connection, """SELECT f.*,s.ticker,s.price FROM market_index_funds f
            JOIN market_securities s ON s.id=f.security_id WHERE f.id=:id AND f.enabled=1""", {"id": fund_id})
        if not fund:
            raise HTTPException(status_code=404, detail="Active FCX index not found")
        previous = all_rows(connection, """SELECT m.security_id,s.ticker,m.weight,m.rank FROM market_index_members m
            JOIN market_securities s ON s.id=m.security_id WHERE m.fund_id=:id ORDER BY m.rank""", {"id": fund_id})
        selected: list[dict[str, Any]] = []
        if security_ids:
            rows = all_rows(connection, """SELECT id,ticker,name,sector,price,issued_shares,volatility FROM market_securities
                WHERE id=ANY(CAST(:ids AS int[])) AND active=1 AND lifecycle_status='active'
                  AND index_eligible=1 AND security_type<>'fund'""", {"ids": security_ids})
            by_id = {int(row["id"]): row for row in rows}
            if len(by_id) != len(security_ids):
                raise HTTPException(status_code=409, detail="Every constituent must be an active, index-eligible company")
            selected = [by_id[security_id] for security_id in security_ids]
        changed = _replace_index_members(connection, fund, selected, timestamp, update_target_size=True)
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]),
            action="market.index.members.replaced", target_type="market_index", target_id=str(fund_id),
            previous={"members": previous}, new={"members": changed}, reason=payload.reason, request=request)
        response = _index_admin_payload(connection)
    return response


@router.post("/admin/indexes/rebalance")
def rebalance_indexes(payload: IndexRebalanceRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    timestamp = utcnow().isoformat()
    fund_ids = list(dict.fromkeys(int(value) for value in payload.fund_ids))
    with transaction() as connection:
        values: dict[str, Any] = {}
        where = " WHERE f.enabled=1"
        if fund_ids:
            where += " AND f.id=ANY(CAST(:ids AS int[]))"
            values["ids"] = fund_ids
        funds = all_rows(connection, f"""SELECT f.*,s.ticker,s.price FROM market_index_funds f
            JOIN market_securities s ON s.id=f.security_id{where} ORDER BY f.fund_key""", values)
        if fund_ids and len(funds) != len(fund_ids):
            raise HTTPException(status_code=404, detail="One or more selected FCX indexes were not found")
        if not funds:
            raise HTTPException(status_code=409, detail="No enabled FCX indexes are available")
        eligible = _eligible_index_securities(connection)
        outcomes: list[dict[str, Any]] = []
        for fund in funds:
            previous = all_rows(connection, """SELECT s.ticker,m.weight,m.rank FROM market_index_members m
                JOIN market_securities s ON s.id=m.security_id WHERE m.fund_id=:id ORDER BY m.rank""", {"id": fund["id"]})
            selected = rank_by_market_cap(eligible, max(1, min(50, int(fund.get("target_size") or 8))))
            changed = _replace_index_members(connection, fund, selected, timestamp, update_target_size=False)
            audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]),
                action="market.index.rebalanced_by_market_cap", target_type="market_index", target_id=str(fund["id"]),
                previous={"members": previous}, new={"members": changed}, reason=payload.reason, request=request)
            outcomes.append({"fund_id": int(fund["id"]), "ticker": fund["ticker"], "constituents": len(changed)})
        response = _index_admin_payload(connection)
    return {**response, "rebalanced": outcomes}


@router.get("/admin/operations")
def market_operations(_: dict[str, Any] = Depends(require_roles("developer", "commissioner", "fcx_admin", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        securities = all_rows(connection, """SELECT s.*,
            EXISTS(SELECT 1 FROM market_security_halts h WHERE h.security_id=s.id AND h.status='active') AS halted,
            EXISTS(SELECT 1 FROM market_security_delistings d WHERE d.security_id=s.id AND d.status='active') AS delisted
            FROM market_securities s WHERE s.active=1 AND s.lifecycle_status='active' ORDER BY s.ticker""")
        programs = all_rows(connection, """SELECT p.*,s.ticker,s.name FROM market_price_programs p
            LEFT JOIN market_securities s ON s.id=p.security_id ORDER BY p.id DESC LIMIT 500""")
        halts = all_rows(connection, """SELECT h.*,s.ticker,s.name FROM market_security_halts h
            JOIN market_securities s ON s.id=h.security_id ORDER BY h.id DESC LIMIT 500""")
        delistings = all_rows(connection, """SELECT d.*,s.ticker,s.name FROM market_security_delistings d
            JOIN market_securities s ON s.id=d.security_id ORDER BY d.id DESC LIMIT 500""")
        funds = all_rows(connection, """SELECT f.*,s.ticker,s.price,
            COUNT(m.security_id) AS constituent_count FROM market_index_funds f
            JOIN market_securities s ON s.id=f.security_id
            LEFT JOIN market_index_members m ON m.fund_id=f.id
            GROUP BY f.id,s.id ORDER BY f.fund_key""")
    return {"ok": True, "securities": securities, "programs": programs, "halts": halts, "delistings": delistings, "funds": funds}


@router.post("/admin/operations/programs")
def create_price_program(payload: PriceProgramRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    starts_at = payload.starts_at or utcnow()
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=timezone.utc)
    ends_at = starts_at + timedelta(minutes=payload.duration_minutes)
    created: list[dict[str, Any]] = []
    with transaction() as connection:
        securities = all_rows(connection, "SELECT id,ticker,name,price FROM market_securities WHERE id=ANY(CAST(:ids AS int[])) ORDER BY ticker", {"ids": payload.security_ids})
        if len(securities) != len(set(payload.security_ids)):
            raise HTTPException(status_code=404, detail="One or more selected FCX securities were not found")
        for security in securities:
            start_price = Decimal(str(security["price"]))
            target_price = (start_price * (Decimal("1") + payload.percent_change / Decimal("100"))).quantize(Decimal("0.0001"))
            status = "scheduled" if starts_at > utcnow() else "active"
            row = one(connection, """INSERT INTO market_price_programs
                (security_id,event_name,percent_change,duration_minutes,start_price,target_price,starts_at,ends_at,status,created_by,created_at)
                VALUES (:security,:event,:change,:duration,:start,:target,:starts,:ends,:status,:user,:now)
                RETURNING id""", {"security": security["id"], "event": payload.event_name, "change": payload.percent_change,
                "duration": payload.duration_minutes, "start": start_price, "target": target_price,
                "starts": starts_at.isoformat(), "ends": ends_at.isoformat(), "status": status, "user": user["id"], "now": utcnow().isoformat()})
            created.append({"id": row["id"], "ticker": security["ticker"], "start_price": start_price, "target_price": target_price, "status": status})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="market.program.created", target_type="security_group", target_id=",".join(str(item) for item in payload.security_ids), new={**payload.model_dump(), "created": created}, request=request)
    return {"ok": True, "programs": created}


@router.post("/admin/operations/programs/{program_id}/stop")
def stop_price_program(program_id: int, payload: ProgramStopRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        program = one(connection, """SELECT p.*,s.ticker,s.price AS current_price FROM market_price_programs p
            LEFT JOIN market_securities s ON s.id=p.security_id WHERE p.id=:id""", {"id": program_id})
        if not program:
            raise HTTPException(status_code=404, detail="Price program not found")
        execute(connection, "UPDATE market_price_programs SET status='stopped',ends_at=:now WHERE id=:id", {"id": program_id, "now": utcnow().isoformat()})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="market.program.stopped", target_type="price_program", target_id=str(program_id), previous=program, new={"keep_current_price": payload.keep_current_price, "current_price": program.get("current_price")}, request=request)
    return {"ok": True, "kept_price": program.get("current_price")}


@router.post("/admin/operations/halts")
def halt_securities(payload: SecurityHaltRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    created: list[int] = []
    with transaction() as connection:
        securities = all_rows(connection, "SELECT id,ticker FROM market_securities WHERE id=ANY(CAST(:ids AS int[]))", {"ids": payload.security_ids})
        if len(securities) != len(set(payload.security_ids)):
            raise HTTPException(status_code=404, detail="One or more selected FCX securities were not found")
        for security in securities:
            existing = one(connection, "SELECT id FROM market_security_halts WHERE security_id=:security AND status='active'", {"security": security["id"]})
            if existing:
                continue
            row = one(connection, """INSERT INTO market_security_halts
                (security_id,status,reason_code,reason_label,public_notice,case_reference,halted_by,halted_by_name,halted_at,automatic_resume_at,engine_managed)
                VALUES (:security,'active',:code,:label,:notice,:case,:user,:name,:now,:resume,0) RETURNING id""",
                {"security": security["id"], "code": payload.reason_code, "label": payload.reason_label,
                 "notice": payload.public_notice, "case": payload.case_reference, "user": user["id"], "name": user["display_name"],
                 "now": utcnow().isoformat(), "resume": payload.automatic_resume_at.isoformat() if payload.automatic_resume_at else None})
            created.append(row["id"])
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.security.halted", target_type="security_group", target_id=",".join(str(item) for item in payload.security_ids), new=payload.model_dump(), request=request)
    return {"ok": True, "created": created}


@router.post("/admin/operations/halts/{halt_id}/resume")
def resume_halt(halt_id: int, payload: ReleaseRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        existing = one(connection, "SELECT * FROM market_security_halts WHERE id=:id AND status='active'", {"id": halt_id})
        if not existing:
            raise HTTPException(status_code=404, detail="Active halt not found")
        execute(connection, """UPDATE market_security_halts SET status='released',resumed_by=:user,resumed_by_name=:name,
            resumed_at=:now,resume_note=:note WHERE id=:id""", {"id": halt_id, "user": user["id"], "name": user["display_name"], "now": utcnow().isoformat(), "note": payload.note})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.security.resumed", target_type="security_halt", target_id=str(halt_id), previous=existing, new={"note": payload.note}, request=request)
    return {"ok": True}


@router.post("/admin/operations/halts/resume-all")
def resume_all_halts(payload: ReleaseRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        active = all_rows(connection, "SELECT id FROM market_security_halts WHERE status='active'")
        execute(connection, """UPDATE market_security_halts SET status='released',resumed_by=:user,resumed_by_name=:name,
            resumed_at=:now,resume_note=:note WHERE status='active'""", {"user": user["id"], "name": user["display_name"], "now": utcnow().isoformat(), "note": payload.note})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.security.resumed_all", target_type="security_halt", new={"count": len(active), "note": payload.note}, request=request)
    return {"ok": True, "released": len(active)}


@router.post("/admin/operations/delistings")
def delist_security(payload: SecurityDelistRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        security = one(connection, "SELECT * FROM market_securities WHERE id=:id", {"id": payload.security_id})
        if not security:
            raise HTTPException(status_code=404, detail="FCX security not found")
        if one(connection, "SELECT id FROM market_security_delistings WHERE security_id=:id AND status='active'", {"id": payload.security_id}):
            raise HTTPException(status_code=409, detail="Security is already delisted")
        row = one(connection, """INSERT INTO market_security_delistings
            (security_id,status,reason_code,reason_label,public_notice,case_reference,delisted_by,delisted_by_name,delisted_at)
            VALUES (:security,'active',:code,:label,:notice,:case,:user,:name,:now) RETURNING id""",
            {"security": payload.security_id, "code": payload.reason_code, "label": payload.reason_label, "notice": payload.public_notice,
             "case": payload.case_reference, "user": user["id"], "name": user["display_name"], "now": utcnow().isoformat()})
        execute(connection, "UPDATE market_securities SET active=0,lifecycle_status='delisted',updated_at=:now WHERE id=:id", {"id": payload.security_id, "now": utcnow().isoformat()})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.security.delisted", target_type="security", target_id=str(payload.security_id), previous=security, new=payload.model_dump(), request=request)
    return {"ok": True, "delisting_id": row["id"]}


@router.post("/admin/operations/delistings/{delisting_id}/relist")
def relist_security(delisting_id: int, payload: ReleaseRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        existing = one(connection, "SELECT * FROM market_security_delistings WHERE id=:id AND status='active'", {"id": delisting_id})
        if not existing:
            raise HTTPException(status_code=404, detail="Active delisting not found")
        execute(connection, """UPDATE market_security_delistings SET status='released',relisted_by=:user,relisted_by_name=:name,
            relisted_at=:now,relist_note=:note WHERE id=:id""", {"id": delisting_id, "user": user["id"], "name": user["display_name"], "now": utcnow().isoformat(), "note": payload.note})
        execute(connection, "UPDATE market_securities SET active=1,lifecycle_status='active',updated_at=:now WHERE id=:id", {"id": existing["security_id"], "now": utcnow().isoformat()})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.security.relisted", target_type="security", target_id=str(existing["security_id"]), previous=existing, new={"note": payload.note}, request=request)
    return {"ok": True}


@router.get("/admin/leverage")
def leverage_workspace(_: dict[str, Any] = Depends(require_roles("developer", "commissioner", "fcx_admin", "fec_admin"))) -> dict[str, Any]:
    identity_select = """r.account_id AS ravenhood_account_id,r.display_name,r.status AS resident_status,
        COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id,
        COALESCE(STRING_AGG(DISTINCT l.community_id, ', '),'') AS communities"""
    with transaction() as connection:
        settings_rows = all_rows(connection, "SELECT setting_key,setting_value,updated_at FROM system_settings WHERE setting_key LIKE '%leverage%' OR setting_key LIKE '%margin%' ORDER BY setting_key")
        securities = all_rows(connection, "SELECT id,ticker,name,price,margin_enabled,margin_max_leverage,active,lifecycle_status FROM market_securities WHERE active=1 AND lifecycle_status='active' ORDER BY ticker")
        open_positions = all_rows(connection, f"""SELECT p.*,s.ticker,s.name,s.price AS mark_price,a.cash_balance,
            {identity_select},
            COALESCE(MAX(ar.scope) FILTER (WHERE ar.status='active'),'') AS restriction_scope
            FROM market_margin_positions p JOIN market_securities s ON s.id=p.security_id
            JOIN market_accounts a ON a.id=p.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            LEFT JOIN market_account_trading_restrictions ar ON ar.account_id=a.id
            WHERE p.status='open' GROUP BY p.id,s.id,a.id,r.id ORDER BY p.opened_at DESC LIMIT 1000""")
        closed_positions = all_rows(connection, f"""SELECT p.*,s.ticker,s.name,a.cash_balance,
            {identity_select} FROM market_margin_positions p JOIN market_securities s ON s.id=p.security_id
            JOIN market_accounts a ON a.id=p.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            WHERE p.status<>'open' GROUP BY p.id,s.id,a.id,r.id ORDER BY p.closed_at DESC NULLS LAST,p.id DESC LIMIT 1000""")
        requests = all_rows(connection, f"""SELECT q.*,s.ticker,s.name,a.cash_balance,
            {identity_select} FROM market_margin_order_requests q JOIN market_securities s ON s.id=q.security_id
            JOIN market_accounts a ON a.id=q.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            GROUP BY q.id,s.id,a.id,r.id ORDER BY q.id DESC LIMIT 1000""")
        stats = one(connection, """SELECT
            COUNT(*) FILTER (WHERE status='open') AS open_positions,
            COALESCE(SUM(collateral) FILTER (WHERE status='open'),0) AS locked_collateral,
            COALESCE(SUM(entry_notional) FILTER (WHERE status='open'),0) AS open_exposure,
            COALESCE(SUM(realized_pnl) FILTER (WHERE status<>'open'),0) AS realized_pnl,
            COUNT(*) FILTER (WHERE status<>'open') AS closed_positions
            FROM market_margin_positions""") or {}
    return {"ok": True, "settings": settings_rows, "securities": securities, "open_positions": open_positions, "closed_positions": closed_positions, "requests": requests, "stats": stats}


@router.patch("/admin/leverage/securities/{security_id}")
def update_security_margin(
    security_id: int,
    payload: SecurityMarginPatch,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin")),
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No security leverage settings supplied")
    assignments = ",".join(f"{key}=:{key}" for key in changes)
    with transaction() as connection:
        previous = one(connection, "SELECT id,ticker,name,margin_enabled,margin_max_leverage FROM market_securities WHERE id=:id", {"id": security_id})
        if not previous:
            raise HTTPException(status_code=404, detail="FCX security not found")
        execute(connection, f"UPDATE market_securities SET {assignments},updated_at=:updated WHERE id=:id", {**changes, "updated": utcnow().isoformat(), "id": security_id})
        current = one(connection, "SELECT id,ticker,name,margin_enabled,margin_max_leverage FROM market_securities WHERE id=:id", {"id": security_id})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="leverage.security.updated", target_type="security", target_id=str(security_id), previous=previous, new=current, request=request)
    return {"ok": True, "security": current}


@router.get("/admin/fec/workspace")
def fec_workspace(_: dict[str, Any] = Depends(require_roles("developer", "fec_admin", "fec_investigator"))) -> dict[str, Any]:
    with transaction() as connection:
        accounts = all_rows(connection, """SELECT r.id,r.account_id,r.display_name,r.status,r.restriction_reason,
            a.id AS market_account_id,a.cash_balance,a.status AS trading_status,
            COALESCE(SUM(h.quantity*s.price),0) AS holdings_value,
            COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id,
            COALESCE(STRING_AGG(DISTINCT l.community_id, ', '),'') AS communities,
            COALESCE(MAX(ar.scope) FILTER (WHERE ar.status='active'),'') AS restriction_scope,
            COALESCE(MAX(ar.reason) FILTER (WHERE ar.status='active'),'') AS restriction_reason,
            COALESCE(MAX(ar.case_reference) FILTER (WHERE ar.status='active'),'') AS restriction_case
            FROM fcx_ravenhood_accounts r LEFT JOIN market_accounts a ON a.user_id=r.id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            LEFT JOIN market_holdings h ON h.account_id=a.id LEFT JOIN market_securities s ON s.id=h.security_id
            LEFT JOIN market_account_trading_restrictions ar ON ar.account_id=a.id
            GROUP BY r.id,a.id ORDER BY (a.cash_balance+COALESCE(SUM(h.quantity*s.price),0)) DESC NULLS LAST LIMIT 1000""")
        investigations_rows = all_rows(connection, """SELECT i.*,r.display_name,
            COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id,
            COALESCE(STRING_AGG(DISTINCT l.community_id, ', '),'') AS communities
            FROM fcx_investigations i JOIN fcx_ravenhood_accounts r ON r.account_id=i.account_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            GROUP BY i.id,r.id ORDER BY i.updated_at DESC LIMIT 1000""")
        restrictions = all_rows(connection, """SELECT ar.*,r.account_id AS ravenhood_account_id,r.display_name,
            COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id
            FROM market_account_trading_restrictions ar JOIN market_accounts a ON a.id=ar.account_id
            JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            GROUP BY ar.id,r.id ORDER BY ar.id DESC LIMIT 1000""")
        executions = all_rows(connection, """SELECT o.id,o.side,o.quantity,o.unit_price,o.gross_amount,o.fee_amount,o.created_at,
            s.ticker,s.name,r.account_id AS ravenhood_account_id,r.display_name,
            COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id
            FROM market_orders o JOIN market_securities s ON s.id=o.security_id
            JOIN market_accounts a ON a.id=o.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            GROUP BY o.id,s.id,r.id ORDER BY o.id DESC LIMIT 2000""")
        system_trades = all_rows(connection, """SELECT t.*,s.ticker,s.name FROM market_system_trades t
            JOIN market_securities s ON s.id=t.security_id ORDER BY t.id DESC LIMIT 1000""")
        settlements = all_rows(connection, "SELECT * FROM fcx_settlements ORDER BY id DESC LIMIT 1000")
        issuers = all_rows(connection, """SELECT c.*,s.price,s.active,s.lifecycle_status,
            COALESCE(s.price*s.issued_shares,0) AS current_market_cap FROM business_issuer_companies c
            LEFT JOIN market_securities s ON s.id=c.security_id ORDER BY c.id DESC LIMIT 500""")
        halts = all_rows(connection, """SELECT h.*,s.ticker,s.name FROM market_security_halts h JOIN market_securities s ON s.id=h.security_id
            WHERE h.status='active' ORDER BY h.id DESC""")
        delistings = all_rows(connection, """SELECT d.*,s.ticker,s.name FROM market_security_delistings d JOIN market_securities s ON s.id=d.security_id
            ORDER BY d.id DESC LIMIT 500""")
        holdings = all_rows(connection, """SELECT h.id,h.account_id,h.quantity,h.reserved_quantity,h.average_cost,
            s.ticker,s.name,s.price,(h.quantity*s.price) AS market_value,
            ((s.price-h.average_cost)*h.quantity) AS unrealized_pnl,
            r.account_id AS ravenhood_account_id,r.display_name,
            COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id
            FROM market_holdings h JOIN market_securities s ON s.id=h.security_id
            JOIN market_accounts a ON a.id=h.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            WHERE h.quantity<>0 GROUP BY h.id,s.id,r.id ORDER BY ABS(h.quantity*s.price) DESC LIMIT 5000""")
        pnl_windows: dict[str, Any] = {}
        for label, hours in (("12h", 12), ("1d", 24), ("1w", 168)):
            cutoff = (utcnow() - timedelta(hours=hours)).isoformat()
            summary = one(connection, """SELECT
                COALESCE((SELECT SUM(realized_pnl) FROM market_margin_positions WHERE status<>'open' AND closed_at>=:cutoff),0) AS realized,
                COALESCE((SELECT SUM(CASE WHEN p.direction='long' THEN (s.price-p.entry_price)*p.quantity
                    ELSE (p.entry_price-s.price)*p.quantity END) FROM market_margin_positions p
                    JOIN market_securities s ON s.id=p.security_id WHERE p.status='open'),0) AS margin_unrealized,
                COALESCE((SELECT SUM((s.price-h.average_cost)*h.quantity) FROM market_holdings h
                    JOIN market_securities s ON s.id=h.security_id),0) AS equity_unrealized,
                COALESCE((SELECT SUM(cash_balance) FROM market_accounts),0) AS cash_equity,
                (SELECT COUNT(*) FROM market_orders WHERE created_at>=:cutoff) AS executions,
                COALESCE((SELECT SUM(gross_amount) FROM market_orders WHERE created_at>=:cutoff),0) AS turnover,
                COALESCE((SELECT SUM(fee_amount) FROM market_orders WHERE created_at>=:cutoff),0) AS fees""", {"cutoff": cutoff}) or {}
            leaders = all_rows(connection, """SELECT r.account_id,r.display_name,
                COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id,
                COALESCE((SELECT SUM(p.realized_pnl) FROM market_margin_positions p WHERE p.account_id=a.id
                    AND p.status<>'open' AND p.closed_at>=:cutoff),0) AS realized,
                COALESCE((SELECT SUM(CASE WHEN p.direction='long' THEN (s.price-p.entry_price)*p.quantity
                    ELSE (p.entry_price-s.price)*p.quantity END) FROM market_margin_positions p
                    JOIN market_securities s ON s.id=p.security_id WHERE p.account_id=a.id AND p.status='open'),0) AS margin_unrealized,
                COALESCE((SELECT SUM((s.price-h.average_cost)*h.quantity) FROM market_holdings h
                    JOIN market_securities s ON s.id=h.security_id WHERE h.account_id=a.id),0) AS equity_unrealized
                FROM market_accounts a JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
                LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
                GROUP BY a.id,r.id ORDER BY (
                    COALESCE((SELECT SUM(p.realized_pnl) FROM market_margin_positions p WHERE p.account_id=a.id AND p.status<>'open' AND p.closed_at>=:cutoff),0)
                    + COALESCE((SELECT SUM(CASE WHEN p.direction='long' THEN (s.price-p.entry_price)*p.quantity ELSE (p.entry_price-s.price)*p.quantity END)
                        FROM market_margin_positions p JOIN market_securities s ON s.id=p.security_id WHERE p.account_id=a.id AND p.status='open'),0)
                    + COALESCE((SELECT SUM((s.price-h.average_cost)*h.quantity) FROM market_holdings h JOIN market_securities s ON s.id=h.security_id WHERE h.account_id=a.id),0)
                ) DESC LIMIT 100""", {"cutoff": cutoff})
            summary["realized_margin"] = summary.get("realized", 0)
            summary["unrealized_margin"] = summary.get("margin_unrealized", 0)
            summary["net_pnl"] = sum((Decimal(str(summary.get(key, 0) or 0)) for key in ("realized", "margin_unrealized", "equity_unrealized")), Decimal("0"))
            for leader in leaders:
                leader["net_pnl"] = sum((Decimal(str(leader.get(key, 0) or 0)) for key in ("realized", "margin_unrealized", "equity_unrealized")), Decimal("0"))
            pnl_windows[label] = {"summary": summary, "leaders": leaders}
        pool = one(connection, "SELECT balance,updated_at FROM market_fec_asset_pool WHERE id=1") or {"balance": 0, "updated_at": ""}
        asset_ledger = all_rows(connection, "SELECT * FROM market_fec_asset_ledger ORDER BY id DESC LIMIT 1000")
        equity_resets = all_rows(connection, "SELECT * FROM market_fec_equity_cash_resets ORDER BY id DESC LIMIT 100")
        share_resets = all_rows(connection, "SELECT * FROM market_fec_share_resets ORDER BY id DESC LIMIT 100")
        ipo_reviews = all_rows(connection, """SELECT c.*,s.price,s.active,s.lifecycle_status,
            COALESCE(s.price*s.issued_shares,0) AS current_market_cap,
            COALESCE(r.display_name,'') AS owner_name,r.account_id AS owner_account_id,
            COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id
            FROM business_issuer_companies c LEFT JOIN market_securities s ON s.id=c.security_id
            LEFT JOIN fcx_ravenhood_accounts r ON r.id=c.controlling_user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            GROUP BY c.id,s.id,r.id ORDER BY CASE c.review_status WHEN 'pending' THEN 0 WHEN 'needs_changes' THEN 1 ELSE 2 END,c.id DESC""")
    return {"ok": True, "accounts": accounts, "investigations": investigations_rows, "restrictions": restrictions,
            "executions": executions, "system_trades": system_trades, "settlements": settlements, "issuers": issuers,
            "halts": halts, "delistings": delistings, "holdings": holdings, "pnl_windows": pnl_windows,
            "asset_pool": pool, "asset_ledger": asset_ledger, "equity_resets": equity_resets,
            "share_resets": share_resets, "ipo_reviews": ipo_reviews}


@router.post("/admin/fec/restrictions")
def restrict_account(payload: AccountRestrictionRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        account = one(connection, """SELECT a.*,r.account_id AS ravenhood_account_id,r.display_name FROM market_accounts a
            JOIN fcx_ravenhood_accounts r ON r.id=a.user_id WHERE a.id=:id""", {"id": payload.account_id})
        if not account:
            raise HTTPException(status_code=404, detail="Ravenhood market account not found")
        existing = one(connection, "SELECT id FROM market_account_trading_restrictions WHERE account_id=:id AND status='active'", {"id": payload.account_id})
        if existing:
            raise HTTPException(status_code=409, detail="This account already has an active FEC restriction")
        row = one(connection, """INSERT INTO market_account_trading_restrictions
            (account_id,scope,status,reason,case_reference,created_by,created_by_name,created_at)
            VALUES (:account,:scope,'active',:reason,:case,:user,:name,:now) RETURNING id""",
            {"account": payload.account_id, "scope": payload.scope, "reason": payload.reason, "case": payload.case_reference,
             "user": user["id"], "name": user["display_name"], "now": utcnow().isoformat()})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.account.restricted", target_type="ravenhood_account", target_id=account["ravenhood_account_id"], new=payload.model_dump(), request=request)
    return {"ok": True, "restriction_id": row["id"]}


@router.post("/admin/fec/restrictions/{restriction_id}/release")
def release_account_restriction(restriction_id: int, payload: ReleaseRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    with transaction() as connection:
        existing = one(connection, "SELECT * FROM market_account_trading_restrictions WHERE id=:id AND status='active'", {"id": restriction_id})
        if not existing:
            raise HTTPException(status_code=404, detail="Active account restriction not found")
        execute(connection, """UPDATE market_account_trading_restrictions SET status='released',released_by=:user,released_by_name=:name,
            released_at=:now,release_note=:note WHERE id=:id""", {"id": restriction_id, "user": user["id"], "name": user["display_name"], "now": utcnow().isoformat(), "note": payload.note})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.account.restriction_released", target_type="account_restriction", target_id=str(restriction_id), previous=existing, new={"note": payload.note}, request=request)
    return {"ok": True}


@router.post("/admin/fec/custody/seize")
def seize_fec_assets(payload: AssetSeizureRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    now = utcnow().isoformat()
    with transaction() as connection:
        locked_account = one(connection, "SELECT id FROM market_accounts WHERE id=:id FOR UPDATE", {"id": payload.account_id})
        if not locked_account:
            raise HTTPException(status_code=404, detail="Ravenhood market account not found")
        account = one(connection, """SELECT a.id,a.user_id,a.cash_balance,r.account_id AS ravenhood_account_id,r.display_name,
            COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id
            FROM market_accounts a JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
            WHERE a.id=:id GROUP BY a.id,r.id""", {"id": payload.account_id})
        if not account:
            raise HTTPException(status_code=404, detail="Ravenhood market account not found")
        available = Decimal(str(account["cash_balance"] or 0))
        if payload.amount > available:
            raise HTTPException(status_code=409, detail="Seizure exceeds settled Ravenhood cash")
        execute(connection, "UPDATE market_accounts SET cash_balance=cash_balance-:amount,updated_at=:now WHERE id=:id", {"amount": payload.amount, "now": now, "id": payload.account_id})
        pool = one(connection, "UPDATE market_fec_asset_pool SET balance=balance+:amount,updated_at=:now WHERE id=1 RETURNING balance", {"amount": payload.amount, "now": now}) or {"balance": payload.amount}
        ledger = one(connection, """INSERT INTO market_fec_asset_ledger
            (event_type,amount,pool_delta,pool_balance_after,market_account_id,target_user_id,target_name,target_account_id,
             target_identity_id,case_reference,reason,created_by,created_by_name,created_at)
            VALUES ('seizure',:amount,:amount,:balance,:account,:user_id,:name,:account_id,:identity,:case,:reason,:actor,:actor_name,:now)
            RETURNING id""", {"amount": payload.amount, "balance": pool["balance"], "account": payload.account_id,
            "user_id": account["user_id"], "name": account["display_name"], "account_id": account["ravenhood_account_id"],
            "identity": account["bohemia_identity_id"], "case": payload.case_reference, "reason": payload.reason,
            "actor": user["id"], "actor_name": user["display_name"], "now": now})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.assets.seized", target_type="ravenhood_account", target_id=account["ravenhood_account_id"], new=payload.model_dump(), request=request)
    return {"ok": True, "ledger_id": ledger["id"], "pool_balance": pool["balance"]}


@router.post("/admin/fec/custody/dispositions")
def dispose_fec_assets(payload: AssetDispositionRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    now = utcnow().isoformat()
    allocations: list[dict[str, Any]] = []
    target: dict[str, Any] = {}
    with transaction() as connection:
        pool = one(connection, "SELECT balance FROM market_fec_asset_pool WHERE id=1 FOR UPDATE") or {"balance": 0}
        if payload.amount > Decimal(str(pool["balance"] or 0)):
            raise HTTPException(status_code=409, detail="Disposition exceeds the FEC custody pool")
        if payload.disposition == "return":
            if not payload.target_account_id:
                raise HTTPException(status_code=400, detail="A cleared resident account is required for return")
            locked_target = one(connection, "SELECT id FROM market_accounts WHERE id=:id FOR UPDATE", {"id": payload.target_account_id})
            if not locked_target:
                raise HTTPException(status_code=404, detail="Return account not found")
            target = one(connection, """SELECT a.id,a.user_id,r.account_id AS ravenhood_account_id,r.display_name,
                COALESCE(MAX(NULLIF(l.bohemia_identity_id,'')),'') AS bohemia_identity_id
                FROM market_accounts a JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
                LEFT JOIN fcx_ravenhood_links l ON l.account_id=r.account_id AND l.active=TRUE
                WHERE a.id=:id GROUP BY a.id,r.id""", {"id": payload.target_account_id}) or {}
            if not target:
                raise HTTPException(status_code=404, detail="Return account not found")
            execute(connection, "UPDATE market_accounts SET cash_balance=cash_balance+:amount,updated_at=:now WHERE id=:id", {"amount": payload.amount, "now": now, "id": payload.target_account_id})
        elif payload.disposition == "reinvest":
            securities = all_rows(connection, """SELECT id,ticker,price,issued_shares,(price*issued_shares) AS market_cap
                FROM market_securities WHERE active=1 AND lifecycle_status='active' AND price>0 AND issued_shares>0 ORDER BY ticker FOR UPDATE""")
            total_cap = sum((Decimal(str(row["market_cap"] or 0)) for row in securities), Decimal("0"))
            if not securities or total_cap <= 0:
                raise HTTPException(status_code=409, detail="No active market capitalization is available for reinvestment")
            remaining = payload.amount
            for index, security in enumerate(securities):
                cap = Decimal(str(security["market_cap"] or 0))
                allocation = remaining if index == len(securities) - 1 else (payload.amount * cap / total_cap).quantize(Decimal("0.01"))
                remaining -= allocation
                old_price = Decimal(str(security["price"]))
                shares = Decimal(str(security["issued_shares"]))
                new_price = ((cap + allocation) / shares).quantize(Decimal("0.0001"))
                volume = (allocation / old_price).quantize(Decimal("0.00000001"))
                change = ((new_price / old_price - 1) * 100).quantize(Decimal("0.0001"))
                execute(connection, "UPDATE market_securities SET price=:price,updated_at=:now WHERE id=:id", {"price": new_price, "now": now, "id": security["id"]})
                execute(connection, "INSERT INTO market_price_history (security_id,price,source,recorded_at) VALUES (:id,:price,'fec_reinvestment',:now)", {"id": security["id"], "price": new_price, "now": now})
                execute(connection, """INSERT INTO market_system_trades
                    (security_id,buy_volume,sell_volume,buy_trade_count,sell_trade_count,reference_price,price_change_percent,source,rationale,created_at)
                    VALUES (:id,:volume,0,1,0,:price,:change,'fec_reinvestment',:reason,:now)""",
                    {"id": security["id"], "volume": volume, "price": new_price, "change": change, "reason": payload.reason, "now": now})
                allocations.append({"ticker": security["ticker"], "amount": str(allocation), "old_price": str(old_price), "new_price": str(new_price)})
        next_pool = Decimal(str(pool["balance"] or 0)) - payload.amount
        execute(connection, "UPDATE market_fec_asset_pool SET balance=:balance,updated_at=:now WHERE id=1", {"balance": next_pool, "now": now})
        ledger = one(connection, """INSERT INTO market_fec_asset_ledger
            (event_type,amount,pool_delta,pool_balance_after,market_account_id,target_user_id,target_name,target_account_id,
             target_identity_id,case_reference,reason,allocation_json,created_by,created_by_name,created_at)
            VALUES (:event,:amount,:delta,:balance,:account,:target_user,:name,:target_account,:identity,:case,:reason,
             CAST(:allocations AS jsonb),:actor,:actor_name,:now) RETURNING id""", {"event": payload.disposition,
            "amount": payload.amount, "delta": -payload.amount, "balance": next_pool, "account": target.get("id"),
            "target_user": target.get("user_id"), "name": target.get("display_name", ""),
            "target_account": target.get("ravenhood_account_id", ""), "identity": target.get("bohemia_identity_id", ""),
            "case": payload.case_reference, "reason": payload.reason, "allocations": json.dumps(allocations),
            "actor": user["id"], "actor_name": user["display_name"], "now": now})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action=f"fec.assets.{payload.disposition}", target_type="fec_asset_pool", target_id="1", new={**payload.model_dump(), "allocations": allocations}, request=request)
    return {"ok": True, "ledger_id": ledger["id"], "pool_balance": next_pool, "allocations": allocations}


@router.post("/admin/fec/ipos/{company_id}/decision")
def review_ipo(company_id: int, payload: IpoReviewRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    now = utcnow().isoformat()
    with transaction() as connection:
        company = one(connection, "SELECT * FROM business_issuer_companies WHERE id=:id FOR UPDATE", {"id": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="IPO filing not found")
        execute(connection, """UPDATE business_issuer_companies SET review_status=:decision,review_note=:note,
            reviewed_at=:now,reviewed_by=:user,updated_at=:now WHERE id=:id""", {"decision": payload.decision,
            "note": payload.note, "now": now, "user": user["id"], "id": company_id})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.ipo.reviewed", target_type="issuer_company", target_id=str(company_id), previous=company, new=payload.model_dump(), request=request)
    return {"ok": True}


@router.post("/admin/fec/resets/equity")
def wipe_fcx_equity(payload: DestructiveResetRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer"))) -> dict[str, Any]:
    if payload.confirmation != "WIPE FCX EQUITY":
        raise HTTPException(status_code=400, detail="Exact equity reset confirmation is required")
    now = utcnow().isoformat()
    with transaction() as connection:
        totals = one(connection, "SELECT COUNT(*) AS accounts,COALESCE(SUM(cash_balance),0) AS amount FROM market_accounts") or {}
        execute(connection, "UPDATE market_accounts SET cash_balance=0,updated_at=:now", {"now": now})
        execute(connection, "INSERT INTO market_fec_equity_cash_resets (affected_accounts,removed_amount,reason,created_by,created_by_name,created_at) VALUES (:accounts,:amount,:reason,:user,:name,:now)", {"accounts": totals.get("accounts", 0), "amount": totals.get("amount", 0), "reason": payload.reason, "user": user["id"], "name": user["display_name"], "now": now})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.equity.reset", target_type="market_accounts", new=totals, reason=payload.reason, request=request)
    return {"ok": True, **totals}


@router.post("/admin/fec/resets/shares")
def wipe_fcx_shares(payload: DestructiveResetRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer"))) -> dict[str, Any]:
    if payload.confirmation != "WIPE FCX SHARES":
        raise HTTPException(status_code=400, detail="Exact share reset confirmation is required")
    now = utcnow().isoformat()
    with transaction() as connection:
        totals = one(connection, "SELECT COUNT(DISTINCT account_id) AS accounts,COALESCE(SUM(quantity),0) AS shares FROM market_holdings") or {}
        execute(connection, "DELETE FROM market_holdings")
        execute(connection, "INSERT INTO market_fec_share_resets (affected_accounts,removed_shares,reason,created_by,created_by_name,created_at) VALUES (:accounts,:shares,:reason,:user,:name,:now)", {"accounts": totals.get("accounts", 0), "shares": totals.get("shares", 0), "reason": payload.reason, "user": user["id"], "name": user["display_name"], "now": now})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="fec.shares.reset", target_type="market_holdings", new=totals, reason=payload.reason, request=request)
    return {"ok": True, **totals}


@router.get("/admin/credentials")
def credentials(_: dict[str, Any] = Depends(require_roles("fcx_admin", "commissioner"))) -> dict[str, Any]:
    with transaction() as connection:
        rows = all_rows(connection, """SELECT credential_id,community_id,scopes_json,active,last_used_at,created_at,revoked_at
            FROM fcx_community_credentials ORDER BY created_at DESC""")
    return {"ok": True, "credentials": rows}


@router.post("/admin/investigations")
def open_investigation(payload: InvestigationRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer", "fec_admin"))) -> dict[str, Any]:
    case_id = "FEC-" + utcnow().strftime("%Y%m%d") + "-" + secrets.token_hex(4).upper()
    with transaction() as connection:
        execute(
            connection,
            """INSERT INTO fcx_investigations
               (case_id,account_id,status,priority,summary,opened_by,created_at,updated_at)
               VALUES (:case,:account,'open',:priority,:summary,:user,:now,:now)""",
            {"case": case_id, "account": payload.account_id, "priority": payload.priority, "summary": payload.summary, "user": user["id"], "now": utcnow()},
        )
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="investigation.opened", target_type="ravenhood_account", target_id=payload.account_id, new={"case_id": case_id, **payload.model_dump()}, request=request)
    return {"ok": True, "case_id": case_id}


@router.get("/community/bootstrap")
def community_bootstrap(principal: dict[str, Any] = Depends(community_principal)) -> dict[str, Any]:
    community = {key: principal[key] for key in ("community_id", "community_name", "trading_enabled", "buy_enabled", "sell_enabled", "account_creation_enabled")}
    return {"ok": True, "community_id": principal["community_id"], "community": community, "api_version": "v1"}


@router.post("/community/ravenhood/resolve")
def resolve_account(payload: ResolveAccountRequest, principal: dict[str, Any] = Depends(require_community_scopes("account:link"))) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    now = utcnow()
    with transaction() as connection:
        link = one(connection, "SELECT * FROM fcx_ravenhood_links WHERE community_id=:community AND community_user_id=:user", {"community": community_id, "user": payload.community_user_id})
        if link:
            account_id = str(link["account_id"])
        else:
            identity_link = None
            if payload.bohemia_identity_id:
                identity_link = one(connection, "SELECT account_id FROM fcx_ravenhood_links WHERE bohemia_identity_id=:identity AND verified=TRUE LIMIT 1", {"identity": payload.bohemia_identity_id})
            account_id = str(identity_link["account_id"]) if identity_link else "RH-" + secrets.token_hex(12).upper()
            if not identity_link:
                if not principal["account_creation_enabled"]:
                    raise HTTPException(status_code=403, detail="Ravenhood account creation is disabled for this community")
                execute(connection, "INSERT INTO fcx_ravenhood_accounts (account_id,display_name,created_at,updated_at) VALUES (:account,:name,:now,:now)", {"account": account_id, "name": payload.display_name, "now": now})
            execute(
                connection,
                """INSERT INTO fcx_ravenhood_links
                   (account_id,community_id,community_user_id,bohemia_identity_id,verified,created_at,updated_at)
                   VALUES (:account,:community,:user,:identity,:verified,:now,:now)""",
                {"account": account_id, "community": community_id, "user": payload.community_user_id, "identity": payload.bohemia_identity_id, "verified": payload.verified, "now": now},
            )
        account = one(connection, "SELECT * FROM fcx_ravenhood_accounts WHERE account_id=:account", {"account": account_id})
        if not account:
            raise HTTPException(status_code=500, detail="Ravenhood account resolution failed")
        execute(
            connection,
            """INSERT INTO market_accounts (user_id,cash_balance,status,created_at,updated_at)
               VALUES (:user_id,0,'active',:created,:updated)
               ON CONFLICT(user_id) DO NOTHING""",
            {"user_id": account["id"], "created": now.isoformat(), "updated": now.isoformat()},
        )
        audit(connection, actor_type="community", actor_id=community_id, action="ravenhood.resolved", target_type="ravenhood_account", target_id=account_id, new={"community_user_id": payload.community_user_id, "verified": payload.verified})
    return {"ok": True, "account": account}


def _settlement_response(connection: Any, settlement_id: str, community_id: str) -> dict[str, Any]:
    row = one(connection, "SELECT * FROM fcx_settlements WHERE settlement_id=:settlement AND community_id=:community", {"settlement": settlement_id, "community": community_id})
    if not row:
        raise HTTPException(status_code=404, detail="Settlement not found")
    return row


def _settlement_metadata(settlement: dict[str, Any]) -> dict[str, Any]:
    raw = settlement.get("request_json") or {}
    if isinstance(raw, dict):
        return raw
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _is_wallet_transfer(settlement: dict[str, Any]) -> bool:
    metadata = _settlement_metadata(settlement)
    return str(metadata.get("kind") or "").lower() == "wallet_transfer"


def _wallet_market_account(connection: Any, account_id: str, *, lock: bool = False) -> dict[str, Any]:
    suffix = " FOR UPDATE OF ma" if lock else ""
    account = one(
        connection,
        """SELECT ma.id AS market_account_id,ma.cash_balance,ma.status
           FROM fcx_ravenhood_accounts ra
           JOIN market_accounts ma ON ma.user_id=ra.id
           WHERE ra.account_id=:account""" + suffix,
        {"account": account_id},
    )
    if not account:
        raise HTTPException(status_code=404, detail="FCX wallet account was not found")
    return account


def _apply_wallet_transfer_state(
    connection: Any,
    settlement: dict[str, Any],
    requested_state: str,
) -> str:
    """Apply wallet effects exactly once and return the persisted settlement state."""
    if not _is_wallet_transfer(settlement):
        return requested_state
    operation = str(settlement.get("operation") or "").lower()
    amount = _money(settlement.get("amount"))
    settlement_id = str(settlement["settlement_id"])
    account = _wallet_market_account(connection, str(settlement["account_id"]), lock=True)

    if operation == "debit" and requested_state in {"BANK_DEBITED", "SETTLED"}:
        if not settlement.get("wallet_applied_at"):
            execute(
                connection,
                "UPDATE market_accounts SET cash_balance=cash_balance+:amount,updated_at=NOW() WHERE id=:id",
                {"amount": amount, "id": account["market_account_id"]},
            )
            execute(
                connection,
                "UPDATE fcx_settlements SET wallet_applied_at=NOW() WHERE settlement_id=:settlement",
                {"settlement": settlement_id},
            )
        return "SETTLED"

    if operation == "credit" and requested_state in {"BANK_CREDITED", "SETTLED"}:
        if not settlement.get("wallet_applied_at"):
            execute(
                connection,
                "UPDATE fcx_settlements SET wallet_applied_at=NOW() WHERE settlement_id=:settlement",
                {"settlement": settlement_id},
            )
        return "SETTLED"

    if operation == "credit" and requested_state in {"FAILED", "REVERSED"}:
        if settlement.get("wallet_reserved_at") and not settlement.get("wallet_applied_at") and not settlement.get("wallet_reversed_at"):
            execute(
                connection,
                "UPDATE market_accounts SET cash_balance=cash_balance+:amount,updated_at=NOW() WHERE id=:id",
                {"amount": amount, "id": account["market_account_id"]},
            )
            execute(
                connection,
                "UPDATE fcx_settlements SET wallet_reversed_at=NOW() WHERE settlement_id=:settlement",
                {"settlement": settlement_id},
            )
    return requested_state


def _prepare_wallet_transfer_retry(connection: Any, settlement: dict[str, Any]) -> None:
    """Re-reserve a restored withdrawal before an operator retries its bridge command."""
    if not _is_wallet_transfer(settlement) or str(settlement.get("operation") or "").lower() != "credit":
        return
    if not settlement.get("wallet_reversed_at") or settlement.get("wallet_applied_at"):
        return
    amount = _money(settlement.get("amount"))
    account = _wallet_market_account(connection, str(settlement["account_id"]), lock=True)
    if _money(account.get("cash_balance")) < amount:
        raise HTTPException(status_code=409, detail="Insufficient FCX wallet cash to retry this withdrawal")
    execute(
        connection,
        "UPDATE market_accounts SET cash_balance=cash_balance-:amount,updated_at=NOW() WHERE id=:id",
        {"amount": amount, "id": account["market_account_id"]},
    )
    execute(
        connection,
        """UPDATE fcx_settlements SET wallet_reserved_at=NOW(),wallet_reversed_at=NULL,
           cancelled_at=NULL,cancel_reason='',failure_code='',failure_message='',updated_at=NOW()
           WHERE settlement_id=:settlement""",
        {"settlement": settlement["settlement_id"]},
    )


def _cancel_settlement(
    connection: Any,
    settlement: dict[str, Any],
    *,
    reason: str,
    actor_id: str,
    actor_role: str,
    request: Request | None = None,
) -> dict[str, Any]:
    current_state = str(settlement.get("state") or "")
    if current_state in {"SETTLED", "REVERSED"}:
        return settlement
    if current_state not in {"CREATED", "BANK_AUTHORIZED", "FAILED"}:
        raise HTTPException(
            status_code=409,
            detail="This command has already moved money and cannot be safely cancelled",
        )
    _apply_wallet_transfer_state(connection, settlement, "REVERSED")
    execute(
        connection,
        """UPDATE fcx_settlements SET state='REVERSED',cancelled_at=NOW(),cancel_reason=:reason,
           failure_code='',failure_message='',updated_at=NOW() WHERE settlement_id=:settlement""",
        {"reason": reason[:1000], "settlement": settlement["settlement_id"]},
    )
    audit(
        connection,
        actor_type="admin",
        actor_id=actor_id,
        actor_role=actor_role,
        action="settlement.cancelled",
        target_type="settlement",
        target_id=str(settlement["settlement_id"]),
        previous={"state": current_state},
        new={"state": "REVERSED"},
        reason=reason,
        request=request,
    )
    return _settlement_response(connection, str(settlement["settlement_id"]), str(settlement["community_id"]))


def _community_bank_bridge(community_id: str) -> tuple[str, str]:
    with transaction() as connection:
        community = one(
            connection,
            "SELECT bank_bridge_url,bank_secret_env FROM fcx_communities WHERE community_id=:community",
            {"community": community_id},
        ) or {}
    bridge_url = str(community.get("bank_bridge_url") or "").rstrip("/")
    secret_env = str(community.get("bank_secret_env") or "")
    bridge_secret = str(os.environ.get(secret_env) or "") if secret_env else ""
    if not bridge_url or not bridge_secret:
        raise HTTPException(status_code=503, detail="Community bank bridge is not configured")
    return bridge_url, bridge_secret


def _bank_bridge_request(
    *,
    community_id: str,
    settlement_id: str,
    method: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str = "",
) -> dict[str, Any]:
    bridge_url, bridge_secret = _community_bank_bridge(community_id)
    data = None
    headers = {"X-FCX-Settlement-Key": bridge_secret}
    if payload is not None:
        data = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        bridge_url + "/api/fcx/settlements" + (f"/{settlement_id}" if method == "GET" else ""),
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            reply = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Community bank bridge request failed") from exc
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise HTTPException(status_code=502, detail="Community bank bridge returned an invalid response")
    return reply


def _apply_bank_reply(
    *,
    community_id: str,
    settlement_id: str,
    reply: dict[str, Any],
    audit_action: str,
) -> dict[str, Any]:
    allowed_states = {
        "BANK_AUTHORIZED", "BANK_DEBITED", "BANK_CREDITED", "SETTLED", "FAILED", "REVERSED",
    }
    next_state = str(reply.get("state") or "BANK_AUTHORIZED").upper()
    if next_state not in allowed_states:
        next_state = "BANK_AUTHORIZED"
    with transaction() as connection:
        current = one(
            connection,
            """SELECT * FROM fcx_settlements
               WHERE settlement_id=:settlement AND community_id=:community
               FOR UPDATE""",
            {"settlement": settlement_id, "community": community_id},
        )
        if not current:
            raise HTTPException(status_code=404, detail="Settlement not found")
        if str(current["state"]) in {"SETTLED", "REVERSED"}:
            return current
        # Bridge callbacks and status refreshes can overlap.  Never let a
        # delayed response move a command backwards after a newer response
        # has already advanced it (for example BANK_DEBITED ->
        # BANK_AUTHORIZED).  This also keeps wallet application strictly
        # one-way while the row is locked.
        state_progress = {
            "CREATED": 0,
            "FAILED": 0,
            "BANK_AUTHORIZED": 1,
            "BANK_DEBITED": 2,
            "BANK_CREDITED": 2,
            "ORDER_EXECUTED": 3,
            "SETTLED": 4,
            "REVERSED": 4,
        }
        current_state = str(current["state"])
        if state_progress.get(next_state, 0) < state_progress.get(current_state, 0):
            return current
        next_state = _apply_wallet_transfer_state(connection, current, next_state)
        execute(
            connection,
            """UPDATE fcx_settlements SET state=:state,bank_reference=:reference,
               response_json=CAST(:response AS jsonb),
               failure_code=:failure_code,failure_message=:failure_message,updated_at=NOW()
               WHERE settlement_id=:settlement AND community_id=:community""",
            {
                "state": next_state,
                "reference": str(reply.get("bank_reference") or "")[:200],
                "response": json.dumps(reply, default=str),
                "failure_code": str(reply.get("failure_code") or "")[:120],
                "failure_message": str(reply.get("failure_message") or "")[:2000],
                "settlement": settlement_id,
                "community": community_id,
            },
        )
        audit(
            connection,
            actor_type="community",
            actor_id=community_id,
            action=audit_action,
            target_type="settlement",
            target_id=settlement_id,
            previous={"state": current["state"]},
            new=reply,
        )
        return _settlement_response(connection, settlement_id, community_id)


@router.post("/community/settlements")
def create_settlement(payload: SettlementRequest, principal: dict[str, Any] = Depends(require_community_scopes("settlement:write"))) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    wallet_transfer = str(payload.metadata.get("kind") or "").lower() == "wallet_transfer"
    if not wallet_transfer and payload.operation == "debit" and (not principal["trading_enabled"] or not principal["buy_enabled"]):
        raise HTTPException(status_code=403, detail="Buying is disabled for this community")
    if not wallet_transfer and payload.operation == "credit" and (not principal["trading_enabled"] or not principal["sell_enabled"]):
        raise HTTPException(status_code=403, detail="Selling is disabled for this community")
    with transaction() as connection:
        existing = one(connection, "SELECT * FROM fcx_settlements WHERE community_id=:community AND idempotency_key=:key", {"community": community_id, "key": payload.idempotency_key})
        if existing:
            return {"ok": True, "idempotent_replay": True, "settlement": existing}
        link = one(connection, "SELECT id FROM fcx_ravenhood_links WHERE account_id=:account AND community_id=:community AND community_user_id=:user", {"account": payload.account_id, "community": community_id, "user": payload.community_user_id})
        if not link:
            raise HTTPException(status_code=403, detail="Ravenhood account is not linked to this community user")
        settlement_id = "fcx_txn_" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")
        wallet_reserved_at = None
        if wallet_transfer and payload.operation == "credit":
            wallet = _wallet_market_account(connection, payload.account_id, lock=True)
            if _money(wallet.get("cash_balance")) < _money(payload.amount):
                raise HTTPException(status_code=409, detail="Insufficient FCX wallet cash for this withdrawal")
            execute(
                connection,
                "UPDATE market_accounts SET cash_balance=cash_balance-:amount,updated_at=NOW() WHERE id=:id",
                {"amount": _money(payload.amount), "id": wallet["market_account_id"]},
            )
            wallet_reserved_at = utcnow()
        execute(
            connection,
            """INSERT INTO fcx_settlements
               (settlement_id,idempotency_key,community_id,account_id,community_user_id,
                operation,amount,currency,state,order_reference,request_json,wallet_reserved_at,created_at,updated_at)
               VALUES (:settlement,:key,:community,:account,:user,:operation,:amount,:currency,
                'CREATED',:order_reference,CAST(:request AS jsonb),:wallet_reserved_at,:now,:now)""",
            {"settlement": settlement_id, "key": payload.idempotency_key, "community": community_id, "account": payload.account_id, "user": payload.community_user_id, "operation": payload.operation, "amount": payload.amount, "currency": payload.currency, "order_reference": payload.order_reference, "request": json.dumps(payload.metadata, default=str), "wallet_reserved_at": wallet_reserved_at, "now": utcnow()},
        )
        audit(connection, actor_type="community", actor_id=community_id, action="settlement.created", target_type="settlement", target_id=settlement_id, new=payload.model_dump())
        result = _settlement_response(connection, settlement_id, community_id)
    return {"ok": True, "idempotent_replay": False, "settlement": result}


@router.get("/community/settlements/{settlement_id}")
def get_settlement(settlement_id: str, principal: dict[str, Any] = Depends(require_community_scopes("settlement:write"))) -> dict[str, Any]:
    with transaction() as connection:
        row = _settlement_response(connection, settlement_id, str(principal["community_id"]))
    return {"ok": True, "settlement": row}


@router.post("/community/settlements/{settlement_id}/execute")
def execute_settlement(settlement_id: str, principal: dict[str, Any] = Depends(require_community_scopes("settlement:write"))) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    with transaction() as connection:
        settlement = _settlement_response(connection, settlement_id, community_id)
        if settlement["state"] in {"BANK_AUTHORIZED", "BANK_DEBITED", "BANK_CREDITED", "ORDER_EXECUTED", "SETTLED"}:
            return {"ok": True, "idempotent_replay": True, "settlement": settlement}
        if settlement["state"] not in {"CREATED", "FAILED"}:
            raise HTTPException(status_code=409, detail=f"Settlement cannot execute from {settlement['state']}")
    bridge_payload = {
        "settlement_id": settlement_id,
        "idempotency_key": settlement["idempotency_key"],
        "community_id": community_id,
        "community_user_id": settlement["community_user_id"],
        "ravenhood_account_id": settlement["account_id"],
        "operation": settlement["operation"],
        "amount": str(settlement["amount"]),
        "currency": settlement["currency"],
        "order_reference": settlement["order_reference"],
    }
    try:
        reply = _bank_bridge_request(
            community_id=community_id,
            settlement_id=settlement_id,
            method="POST",
            payload=bridge_payload,
            idempotency_key=str(settlement["idempotency_key"]),
        )
    except HTTPException as exc:
        with transaction() as connection:
            current = one(
                connection,
                """SELECT * FROM fcx_settlements
                   WHERE settlement_id=:settlement AND community_id=:community
                   FOR UPDATE""",
                {"settlement": settlement_id, "community": community_id},
            )
            if not current:
                raise HTTPException(status_code=404, detail="Settlement not found")
            failed_state = _apply_wallet_transfer_state(connection, current, "FAILED")
            execute(connection, "UPDATE fcx_settlements SET state=:state,failure_code='BANK_BRIDGE_UNAVAILABLE',failure_message=:message,updated_at=NOW() WHERE settlement_id=:settlement AND state IN ('CREATED','FAILED')", {"state": failed_state, "message": str(exc)[:2000], "settlement": settlement_id})
        raise
    result = _apply_bank_reply(
        community_id=community_id,
        settlement_id=settlement_id,
        reply=reply,
        audit_action="settlement.bank_authorized",
    )
    return {"ok": True, "idempotent_replay": False, "settlement": result}


@router.post("/community/settlements/{settlement_id}/refresh")
def refresh_settlement(settlement_id: str, principal: dict[str, Any] = Depends(require_community_scopes("settlement:write"))) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    with transaction() as connection:
        current = _settlement_response(connection, settlement_id, community_id)
    if str(current["state"]) in {"SETTLED", "REVERSED"}:
        return {"ok": True, "idempotent_replay": True, "settlement": current}
    reply = _bank_bridge_request(
        community_id=community_id,
        settlement_id=settlement_id,
        method="GET",
    )
    updated = _apply_bank_reply(
        community_id=community_id,
        settlement_id=settlement_id,
        reply=reply,
        audit_action="settlement.bank_status_refreshed",
    )
    return {"ok": True, "idempotent_replay": str(current["state"]) == str(updated["state"]), "settlement": updated}


def reconcile_authorized_settlements(limit: int = 50) -> dict[str, int]:
    """Advance bank-authorized commands after the CAD bridge finishes them.

    The CAD adapter can only acknowledge a newly queued Bank Bridge command as
    ``BANK_AUTHORIZED``.  Its final debit/credit state arrives later, after the
    game bridge has processed the command.  This reconciler deliberately reads
    candidates in one short transaction, performs each network request outside
    that transaction, and relies on ``_apply_bank_reply``'s row lock plus the
    wallet timestamp fields for exactly-once wallet application.
    """
    batch_size = max(1, min(int(limit), 200))
    with transaction() as connection:
        candidates = all_rows(
            connection,
            """SELECT settlement_id,community_id
               FROM fcx_settlements
               WHERE state='BANK_AUTHORIZED'
               ORDER BY updated_at ASC,id ASC
               LIMIT :limit""",
            {"limit": batch_size},
        )

    result = {"checked": 0, "advanced": 0, "settled": 0, "pending": 0, "errors": 0}
    for candidate in candidates:
        settlement_id = str(candidate.get("settlement_id") or "")
        community_id = str(candidate.get("community_id") or "")
        if not settlement_id or not community_id:
            result["errors"] += 1
            continue
        result["checked"] += 1
        try:
            reply = _bank_bridge_request(
                community_id=community_id,
                settlement_id=settlement_id,
                method="GET",
            )
            reply_state = str(reply.get("state") or "BANK_AUTHORIZED").upper()
            # Avoid rewriting the settlement and audit log while the game-side
            # command is legitimately still waiting for its player/bridge.
            if reply_state == "BANK_AUTHORIZED":
                result["pending"] += 1
                continue
            updated = _apply_bank_reply(
                community_id=community_id,
                settlement_id=settlement_id,
                reply=reply,
                audit_action="settlement.automatically_reconciled",
            )
            result["advanced"] += 1
            if str(updated.get("state") or "").upper() == "SETTLED":
                result["settled"] += 1
        except HTTPException:
            # A transient CAD/bridge failure must not reverse, duplicate, or
            # fail a command that may already have moved money in game.
            result["errors"] += 1
        except Exception:
            result["errors"] += 1
    return result


@router.post("/community/settlements/{settlement_id}/callback")
def settlement_callback(settlement_id: str, payload: SettlementCallback, principal: dict[str, Any] = Depends(require_community_scopes("settlement:write"))) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    transitions = {
        "CREATED": {"BANK_AUTHORIZED", "BANK_DEBITED", "BANK_CREDITED", "FAILED"},
        "BANK_AUTHORIZED": {"BANK_DEBITED", "BANK_CREDITED", "FAILED"},
        "BANK_DEBITED": {"ORDER_EXECUTED", "SETTLED", "REVERSED", "FAILED"},
        "BANK_CREDITED": {"ORDER_EXECUTED", "SETTLED", "REVERSED", "FAILED"},
        "ORDER_EXECUTED": {"SETTLED", "REVERSED", "FAILED"},
        "FAILED": {"BANK_AUTHORIZED", "BANK_DEBITED", "BANK_CREDITED", "REVERSED"},
        "SETTLED": set(), "REVERSED": set(),
    }
    with transaction() as connection:
        current = _settlement_response(connection, settlement_id, community_id)
    if current["state"] == payload.state:
        return {"ok": True, "idempotent_replay": True, "settlement": current}
    if payload.state not in transitions.get(str(current["state"]), set()):
        raise HTTPException(status_code=409, detail=f"Invalid settlement transition {current['state']} -> {payload.state}")
    updated = _apply_bank_reply(
        community_id=community_id,
        settlement_id=settlement_id,
        reply={
            "state": payload.state,
            "bank_reference": payload.bank_reference,
            "failure_code": payload.failure_code,
            "failure_message": payload.failure_message,
            **payload.response,
        },
        audit_action="settlement.state_changed",
    )
    return {"ok": True, "idempotent_replay": False, "settlement": updated}


@router.post("/admin/settlements/{settlement_id}/retry")
def admin_retry_settlement(
    settlement_id: str,
    payload: SettlementAdminAction,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf(*ADMIN_ROLES)),
) -> dict[str, Any]:
    with transaction() as connection:
        settlement = one(connection, "SELECT * FROM fcx_settlements WHERE settlement_id=:settlement FOR UPDATE", {"settlement": settlement_id})
        if not settlement:
            raise HTTPException(status_code=404, detail="Settlement not found")
        state = str(settlement.get("state") or "")
        if state in {"SETTLED", "REVERSED"}:
            raise HTTPException(status_code=409, detail=f"A {state.lower()} command cannot be retried")
        if state not in {"CREATED", "FAILED", "BANK_AUTHORIZED"}:
            raise HTTPException(status_code=409, detail="This command has already moved money; refresh its status instead")
        _prepare_wallet_transfer_retry(connection, settlement)
        execute(
            connection,
            "UPDATE fcx_settlements SET state=:state,cancelled_at=NULL,cancel_reason='',failure_code='',failure_message='',updated_at=NOW() WHERE settlement_id=:settlement",
            {"state": "BANK_AUTHORIZED" if state == "BANK_AUTHORIZED" else "CREATED", "settlement": settlement_id},
        )
        community_id = str(settlement["community_id"])
        settlement = _settlement_response(connection, settlement_id, community_id)

    if state == "BANK_AUTHORIZED":
        reply = _bank_bridge_request(community_id=community_id, settlement_id=settlement_id, method="GET")
    else:
        reply = _bank_bridge_request(
            community_id=community_id,
            settlement_id=settlement_id,
            method="POST",
            idempotency_key=str(settlement["idempotency_key"]),
            payload={
                "settlement_id": settlement_id,
                "idempotency_key": settlement["idempotency_key"],
                "community_id": community_id,
                "community_user_id": settlement["community_user_id"],
                "ravenhood_account_id": settlement["account_id"],
                "operation": settlement["operation"],
                "amount": str(settlement["amount"]),
                "currency": settlement["currency"],
                "order_reference": settlement["order_reference"],
            },
        )
    updated = _apply_bank_reply(
        community_id=community_id,
        settlement_id=settlement_id,
        reply=reply,
        audit_action="settlement.operator_retried",
    )
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="settlement.retry_requested", target_type="settlement", target_id=settlement_id, reason=payload.reason, request=request)
    return {"ok": True, "settlement": updated}


@router.post("/admin/settlements/{settlement_id}/refresh")
def admin_refresh_settlement(
    settlement_id: str,
    payload: SettlementAdminAction,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf(*ADMIN_ROLES)),
) -> dict[str, Any]:
    with transaction() as connection:
        settlement = one(connection, "SELECT * FROM fcx_settlements WHERE settlement_id=:settlement", {"settlement": settlement_id})
    if not settlement:
        raise HTTPException(status_code=404, detail="Settlement not found")
    if str(settlement["state"]) in {"SETTLED", "REVERSED"}:
        return {"ok": True, "settlement": settlement}
    community_id = str(settlement["community_id"])
    reply = _bank_bridge_request(community_id=community_id, settlement_id=settlement_id, method="GET")
    updated = _apply_bank_reply(community_id=community_id, settlement_id=settlement_id, reply=reply, audit_action="settlement.operator_refreshed")
    with transaction() as connection:
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="settlement.refresh_requested", target_type="settlement", target_id=settlement_id, reason=payload.reason, request=request)
    return {"ok": True, "settlement": updated}


@router.post("/admin/settlements/{settlement_id}/cancel")
def admin_cancel_settlement(
    settlement_id: str,
    payload: SettlementAdminAction,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf(*ADMIN_ROLES)),
) -> dict[str, Any]:
    with transaction() as connection:
        settlement = one(connection, "SELECT * FROM fcx_settlements WHERE settlement_id=:settlement FOR UPDATE", {"settlement": settlement_id})
        if not settlement:
            raise HTTPException(status_code=404, detail="Settlement not found")
        updated = _cancel_settlement(connection, settlement, reason=payload.reason, actor_id=str(user["id"]), actor_role=",".join(user["roles"]), request=request)
    return {"ok": True, "settlement": updated}


@router.post("/admin/settlements/bulk-cancel")
def admin_bulk_cancel_settlements(
    payload: SettlementBulkAction,
    request: Request,
    user: dict[str, Any] = Depends(require_admin_csrf(*ADMIN_ROLES)),
) -> dict[str, Any]:
    cancelled: list[str] = []
    skipped: list[dict[str, str]] = []
    with transaction() as connection:
        if payload.settlement_ids:
            candidates = []
            for settlement_id in payload.settlement_ids:
                row = one(connection, "SELECT * FROM fcx_settlements WHERE settlement_id=:settlement FOR UPDATE", {"settlement": settlement_id})
                if row:
                    candidates.append(row)
        else:
            params: dict[str, Any] = {}
            community_clause = ""
            if payload.community_id:
                community_clause = " AND community_id=:community"
                params["community"] = payload.community_id
            candidates = all_rows(connection, "SELECT * FROM fcx_settlements WHERE state IN ('CREATED','BANK_AUTHORIZED','FAILED')" + community_clause + " ORDER BY id DESC LIMIT 500 FOR UPDATE", params)
        for settlement in candidates:
            try:
                _cancel_settlement(connection, settlement, reason=payload.reason, actor_id=str(user["id"]), actor_role=",".join(user["roles"]), request=request)
                cancelled.append(str(settlement["settlement_id"]))
            except HTTPException as exc:
                skipped.append({"settlement_id": str(settlement["settlement_id"]), "reason": str(exc.detail)})
    return {"ok": True, "cancelled": cancelled, "skipped": skipped, "cancelled_count": len(cancelled)}


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _quantity(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.00000001"))


def _setting_decimal(connection: Any, key: str, default: str) -> Decimal:
    row = one(connection, "SELECT setting_value FROM system_settings WHERE setting_key=:key", {"key": key})
    try:
        return Decimal(str((row or {}).get("setting_value") or default))
    except (ValueError, ArithmeticError):
        return Decimal(default)


def _setting_enabled(connection: Any, key: str, default: bool = True) -> bool:
    row = one(connection, "SELECT setting_value FROM system_settings WHERE setting_key=:key", {"key": key})
    if not row:
        return default
    return str(row.get("setting_value") or "").strip().lower() in {"1", "true", "yes", "on", "open", "enabled"}


def _normalize_trading_restriction_scope(value: Any) -> str:
    """Translate legacy and UI restriction labels into the FCX enforcement lanes."""
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"full", "all", "account", "account_wide"}:
        return "full"
    if normalized in {"equity", "share", "shares", "share_trading", "stock", "stocks"}:
        return "equity"
    if normalized in {"leverage", "leveraged", "margin", "leverage_trading"}:
        return "leverage"
    # Unknown historical scopes must remain restrictive until an investigator clears them.
    return "full"


def _restriction_blocks_lane(scope: Any, lane: str) -> bool:
    restriction_scope = _normalize_trading_restriction_scope(scope)
    requested_lane = _normalize_trading_restriction_scope(lane)
    return restriction_scope == "full" or restriction_scope == requested_lane


def _linked_market_account(
    connection: Any,
    *,
    community_id: str,
    community_user_id: str,
    account_id: str,
    lock: bool = False,
    enforce_restrictions: bool = True,
    restriction_lane: str = "full",
) -> dict[str, Any]:
    suffix = " FOR UPDATE OF ma" if lock else ""
    row = one(
        connection,
        """SELECT ra.id AS ravenhood_internal_id,ra.account_id,ra.display_name,
                  ra.status AS ravenhood_status,rl.verified,rl.active AS link_active,
                  ma.id AS market_account_id,ma.status AS market_status,ma.cash_balance
           FROM fcx_ravenhood_links rl
           JOIN fcx_ravenhood_accounts ra ON ra.account_id=rl.account_id
           JOIN market_accounts ma ON ma.user_id=ra.id
           WHERE rl.community_id=:community AND rl.community_user_id=:user
             AND rl.account_id=:account""" + suffix,
        {"community": community_id, "user": community_user_id, "account": account_id},
    )
    if not row:
        raise HTTPException(status_code=403, detail="Ravenhood account is not linked to this community user")
    if not row.get("verified") or not row.get("link_active"):
        raise HTTPException(status_code=403, detail="A verified active Ravenhood link is required")
    restrictions = all_rows(
        connection,
        """SELECT id,scope,reason,created_at FROM market_account_trading_restrictions
           WHERE account_id=:account AND status='active' ORDER BY id DESC""",
        {"account": row["market_account_id"]},
    )
    for restriction in restrictions:
        restriction["scope"] = _normalize_trading_restriction_scope(restriction.get("scope"))
    blocking_restrictions = [
        restriction
        for restriction in restrictions
        if _restriction_blocks_lane(restriction.get("scope"), restriction_lane)
    ]
    equity_restriction = next(
        (restriction for restriction in restrictions if _restriction_blocks_lane(restriction.get("scope"), "equity")),
        None,
    )
    leverage_restriction = next(
        (restriction for restriction in restrictions if _restriction_blocks_lane(restriction.get("scope"), "leverage")),
        None,
    )
    row["trading_restrictions"] = restrictions
    row["is_restricted"] = bool(restrictions)
    row["equity_restricted"] = equity_restriction is not None
    row["leverage_restricted"] = leverage_restriction is not None
    row["restriction_scope"] = str((blocking_restrictions[0] if blocking_restrictions else restrictions[0] if restrictions else {}).get("scope") or "")
    row["restriction_reason"] = str((blocking_restrictions[0] if blocking_restrictions else restrictions[0] if restrictions else {}).get("reason") or "")
    if enforce_restrictions and blocking_restrictions:
        lane_label = "share" if _normalize_trading_restriction_scope(restriction_lane) == "equity" else "leverage"
        raise HTTPException(status_code=403, detail=f"Ravenhood {lane_label} trading is restricted by FEC")
    return row


def _security_for_trade(connection: Any, ticker: str, *, side: str, lock: bool = False) -> dict[str, Any]:
    suffix = " FOR UPDATE" if lock else ""
    security = one(
        connection,
        """SELECT s.*,
                  EXISTS(SELECT 1 FROM market_security_halts h WHERE h.security_id=s.id AND h.status='active') AS halted,
                  EXISTS(SELECT 1 FROM market_security_delistings d WHERE d.security_id=s.id AND d.status='active') AS delisted
           FROM market_securities s WHERE UPPER(s.ticker)=UPPER(:ticker)""" + suffix,
        {"ticker": ticker},
    )
    if not security or not int(security.get("active") or 0) or str(security.get("lifecycle_status") or "") != "active":
        raise HTTPException(status_code=404, detail="Security is not available for trading")
    if security.get("halted") or security.get("delisted"):
        raise HTTPException(status_code=409, detail="Security trading is halted")
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="Invalid order side")
    return security


def _assert_market_access(connection: Any, principal: dict[str, Any], side: str) -> None:
    if not principal.get("trading_enabled"):
        raise HTTPException(status_code=403, detail="FCX trading is disabled for this community")
    if side == "buy" and not principal.get("buy_enabled"):
        raise HTTPException(status_code=403, detail="FCX buying is disabled for this community")
    if side == "sell" and not principal.get("sell_enabled"):
        raise HTTPException(status_code=403, detail="FCX selling is disabled for this community")
    if not _setting_enabled(connection, "market_open", True):
        raise HTTPException(status_code=409, detail="FCX market is closed")
    if not _setting_enabled(connection, f"{side}_enabled", True):
        raise HTTPException(status_code=409, detail=f"FCX {side} orders are disabled")
    if _setting_enabled(connection, "maintenance_mode", False):
        raise HTTPException(status_code=503, detail="FCX is in maintenance mode")


def _trade_response(connection: Any, trade_request_id: str, community_id: str) -> dict[str, Any]:
    trade = one(
        connection,
        """SELECT tr.*,s.ticker,s.name AS security_name,fs.state AS settlement_state,
                  fs.bank_reference,fs.failure_code AS settlement_failure_code,
                  fs.failure_message AS settlement_failure_message
           FROM fcx_trade_requests tr
           JOIN market_securities s ON s.id=tr.security_id
           LEFT JOIN fcx_settlements fs ON fs.settlement_id=CASE
                WHEN tr.side='buy' THEN NULLIF(tr.debit_settlement_id,'')
                ELSE NULLIF(tr.credit_settlement_id,'') END
           WHERE tr.trade_request_id=:request AND tr.community_id=:community""",
        {"request": trade_request_id, "community": community_id},
    )
    if not trade:
        raise HTTPException(status_code=404, detail="Trade request not found")
    return trade


def _create_trade_settlement(
    connection: Any,
    *,
    trade: dict[str, Any],
    operation: str,
    amount: Decimal,
) -> str:
    settlement_id = "fcx_txn_" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")
    key = f"{trade['trade_request_id']}:{operation}"
    execute(
        connection,
        """INSERT INTO fcx_settlements
           (settlement_id,idempotency_key,community_id,account_id,community_user_id,
            operation,amount,currency,state,order_reference,request_json,created_at,updated_at)
           VALUES (:settlement,:key,:community,:account,:user,:operation,:amount,'FC',
            'CREATED',:order_reference,CAST(:request AS jsonb),:now,:now)""",
        {
            "settlement": settlement_id,
            "key": key,
            "community": trade["community_id"],
            "account": trade["ravenhood_account_id"],
            "user": trade["community_user_id"],
            "operation": operation,
            "amount": amount,
            "order_reference": trade["trade_request_id"],
            "request": json.dumps({"trade_request_id": trade["trade_request_id"], "side": trade["side"]}),
            "now": utcnow(),
        },
    )
    return settlement_id


def _send_trade_settlement(community_id: str, settlement_id: str) -> dict[str, Any]:
    with transaction() as connection:
        settlement = _settlement_response(connection, settlement_id, community_id)
    bridge_payload = {
        "settlement_id": settlement_id,
        "idempotency_key": settlement["idempotency_key"],
        "community_id": community_id,
        "community_user_id": settlement["community_user_id"],
        "ravenhood_account_id": settlement["account_id"],
        "operation": settlement["operation"],
        "amount": str(settlement["amount"]),
        "currency": settlement["currency"],
        "order_reference": settlement["order_reference"],
    }
    method = "POST" if str(settlement["state"]) in {"CREATED", "FAILED"} else "GET"
    try:
        reply = _bank_bridge_request(
            community_id=community_id,
            settlement_id=settlement_id,
            method=method,
            payload=bridge_payload if method == "POST" else None,
            idempotency_key=str(settlement["idempotency_key"]),
        )
    except HTTPException as exc:
        with transaction() as connection:
            execute(
                connection,
                """UPDATE fcx_settlements SET failure_code='BANK_BRIDGE_UNAVAILABLE',
                   failure_message=:message,updated_at=NOW()
                   WHERE settlement_id=:settlement AND state NOT IN ('SETTLED','REVERSED')""",
                {"message": str(exc.detail)[:2000], "settlement": settlement_id},
            )
        raise
    return _apply_bank_reply(
        community_id=community_id,
        settlement_id=settlement_id,
        reply=reply,
        audit_action="trade.settlement_status_refreshed",
    )


def _execute_buy_after_debit(trade_request_id: str, community_id: str) -> None:
    with transaction() as connection:
        trade = one(
            connection,
            "SELECT * FROM fcx_trade_requests WHERE trade_request_id=:request AND community_id=:community FOR UPDATE",
            {"request": trade_request_id, "community": community_id},
        )
        if not trade or trade.get("executed_order_id"):
            return
        settlement = _settlement_response(connection, str(trade["debit_settlement_id"]), community_id)
        if str(settlement["state"]) != "BANK_DEBITED":
            return
        price = Decimal(str(trade["submitted_price"]))
        quantity = _quantity(trade["quantity"])
        gross = _money(price * quantity)
        fee = _money(trade["estimated_fee"])
        order = one(
            connection,
            """INSERT INTO market_orders
               (account_id,security_id,side,quantity,unit_price,gross_amount,fee_amount,created_at)
               VALUES (:account,:security,'buy',:quantity,:price,:gross,:fee,:now)
               RETURNING id""",
            {"account": trade["market_account_id"], "security": trade["security_id"], "quantity": quantity, "price": price, "gross": gross, "fee": fee, "now": utcnow().isoformat()},
        )
        holding = one(
            connection,
            "SELECT * FROM market_holdings WHERE account_id=:account AND security_id=:security FOR UPDATE",
            {"account": trade["market_account_id"], "security": trade["security_id"]},
        )
        if holding:
            old_quantity = _quantity(holding["quantity"])
            new_quantity = old_quantity + quantity
            new_cost = Decimal("0") if new_quantity <= 0 else (
                (old_quantity * Decimal(str(holding["average_cost"]))) + (quantity * price)
            ) / new_quantity
            execute(
                connection,
                "UPDATE market_holdings SET quantity=:quantity,average_cost=:cost WHERE id=:id",
                {"quantity": new_quantity, "cost": new_cost.quantize(Decimal("0.0001")), "id": holding["id"]},
            )
        else:
            execute(
                connection,
                """INSERT INTO market_holdings
                   (account_id,security_id,quantity,reserved_quantity,average_cost)
                   VALUES (:account,:security,:quantity,0,:cost)""",
                {"account": trade["market_account_id"], "security": trade["security_id"], "quantity": quantity, "cost": price},
            )
        execute(
            connection,
            """UPDATE fcx_trade_requests SET status='SETTLED',executed_order_id=:order,
               executed_price=:price,executed_gross=:gross,executed_fee=:fee,
               executed_at=NOW(),settled_at=NOW(),updated_at=NOW()
               WHERE id=:id""",
            {"order": order["id"], "price": price, "gross": gross, "fee": fee, "id": trade["id"]},
        )
        execute(
            connection,
            "UPDATE fcx_settlements SET state='SETTLED',updated_at=NOW() WHERE settlement_id=:settlement",
            {"settlement": trade["debit_settlement_id"]},
        )
        audit(connection, actor_type="system", actor_id="fcx-trade", action="trade.buy_settled", target_type="trade_request", target_id=trade_request_id, new={"order_id": order["id"], "gross": str(gross), "fee": str(fee)})


def _execute_sell_and_create_credit(trade_request_id: str, community_id: str) -> str:
    with transaction() as connection:
        trade = one(
            connection,
            "SELECT * FROM fcx_trade_requests WHERE trade_request_id=:request AND community_id=:community FOR UPDATE",
            {"request": trade_request_id, "community": community_id},
        )
        if not trade:
            raise HTTPException(status_code=404, detail="Trade request not found")
        if trade.get("credit_settlement_id"):
            return str(trade["credit_settlement_id"])
        quantity = _quantity(trade["quantity"])
        price = Decimal(str(trade["submitted_price"]))
        gross = _money(quantity * price)
        fee = _money(trade["estimated_fee"])
        proceeds = max(Decimal("0.01"), gross - fee)
        holding = one(
            connection,
            "SELECT * FROM market_holdings WHERE account_id=:account AND security_id=:security FOR UPDATE",
            {"account": trade["market_account_id"], "security": trade["security_id"]},
        )
        if not holding or _quantity(holding["reserved_quantity"]) < quantity:
            raise HTTPException(status_code=409, detail="Reserved position is no longer available")
        order = one(
            connection,
            """INSERT INTO market_orders
               (account_id,security_id,side,quantity,unit_price,gross_amount,fee_amount,created_at)
               VALUES (:account,:security,'sell',:quantity,:price,:gross,:fee,:now)
               RETURNING id""",
            {"account": trade["market_account_id"], "security": trade["security_id"], "quantity": quantity, "price": price, "gross": gross, "fee": fee, "now": utcnow().isoformat()},
        )
        execute(
            connection,
            """UPDATE market_holdings SET quantity=GREATEST(0,quantity-:quantity),
               reserved_quantity=GREATEST(0,reserved_quantity-:quantity) WHERE id=:id""",
            {"quantity": quantity, "id": holding["id"]},
        )
        settlement_id = _create_trade_settlement(connection, trade=trade, operation="credit", amount=proceeds)
        execute(
            connection,
            """UPDATE fcx_trade_requests SET status='PAYOUT_PENDING',credit_settlement_id=:settlement,
               executed_order_id=:order,executed_price=:price,executed_gross=:gross,
               executed_fee=:fee,executed_at=NOW(),updated_at=NOW() WHERE id=:id""",
            {"settlement": settlement_id, "order": order["id"], "price": price, "gross": gross, "fee": fee, "id": trade["id"]},
        )
        audit(connection, actor_type="system", actor_id="fcx-trade", action="trade.sell_executed", target_type="trade_request", target_id=trade_request_id, new={"order_id": order["id"], "proceeds": str(proceeds)})
        return settlement_id


def _finalize_sell_credit(trade_request_id: str, community_id: str) -> None:
    with transaction() as connection:
        trade = one(
            connection,
            "SELECT * FROM fcx_trade_requests WHERE trade_request_id=:request AND community_id=:community FOR UPDATE",
            {"request": trade_request_id, "community": community_id},
        )
        if not trade or str(trade["status"]) == "SETTLED" or not trade.get("credit_settlement_id"):
            return
        settlement = _settlement_response(connection, str(trade["credit_settlement_id"]), community_id)
        if str(settlement["state"]) != "BANK_CREDITED":
            return
        execute(connection, "UPDATE fcx_trade_requests SET status='SETTLED',settled_at=NOW(),updated_at=NOW() WHERE id=:id", {"id": trade["id"]})
        execute(connection, "UPDATE fcx_settlements SET state='SETTLED',updated_at=NOW() WHERE settlement_id=:settlement", {"settlement": trade["credit_settlement_id"]})
        audit(connection, actor_type="system", actor_id="fcx-trade", action="trade.sell_settled", target_type="trade_request", target_id=trade_request_id, new={"bank_reference": settlement.get("bank_reference", "")})


MARKET_HISTORY_WINDOWS: dict[str, timedelta] = {
    "live": timedelta(hours=6),
    "15m": timedelta(minutes=15),
    "15min": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1hour": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1day": timedelta(days=1),
    "1w": timedelta(days=7),
    "1week": timedelta(days=7),
    "1m": timedelta(days=30),
    "1month": timedelta(days=30),
    "1y": timedelta(days=365),
    "1year": timedelta(days=365),
}


def _sample_history(rows: list[dict[str, Any]], maximum: int = 720) -> list[dict[str, Any]]:
    if len(rows) <= maximum:
        return rows
    step = (len(rows) - 1) / float(maximum - 1)
    indexes = sorted({round(index * step) for index in range(maximum)})
    return [rows[index] for index in indexes]


@router.get("/community/market")
def community_market(
    ticker: str = "",
    history_range: str = "live",
    principal: dict[str, Any] = Depends(require_community_scopes("market:read")),
) -> dict[str, Any]:
    normalized_range = str(history_range or "live").strip().lower()
    if normalized_range not in MARKET_HISTORY_WINDOWS:
        raise HTTPException(status_code=400, detail="Unsupported market history range")
    selected_ticker = str(ticker or "").strip().upper()
    history_now = utcnow()
    history_since = history_now - MARKET_HISTORY_WINDOWS[normalized_range]
    with transaction() as connection:
        securities = all_rows(
            connection,
            """SELECT s.id,s.ticker,s.name,s.security_type,s.sector,s.description,s.price,
                      s.previous_price,s.volatility,s.issued_shares,
                      ROUND(s.price*s.issued_shares,2) AS market_cap,s.updated_at,
                      EXISTS(SELECT 1 FROM market_security_halts h WHERE h.security_id=s.id AND h.status='active') AS halted
               FROM market_securities s
               WHERE s.active=1 AND s.lifecycle_status='active'
                 AND NOT EXISTS(SELECT 1 FROM market_security_delistings d WHERE d.security_id=s.id AND d.status='active')
               ORDER BY s.ticker""",
        )
        settings_rows = all_rows(connection, "SELECT setting_key,setting_value FROM system_settings WHERE setting_key IN ('market_open','buy_enabled','sell_enabled','maintenance_mode')")
        raw_trade_tape = all_rows(
            connection,
            """SELECT t.id,s.ticker,s.name AS security_name,t.buy_volume,t.sell_volume,
                      t.buy_trade_count,t.sell_trade_count,t.reference_price,
                      t.price_change_percent,t.source,t.created_at
               FROM market_system_trades t
               JOIN market_securities s ON s.id=t.security_id
               WHERE s.active=1 AND s.lifecycle_status='active'
                 AND (COALESCE(t.buy_volume,0)>0 OR COALESCE(t.sell_volume,0)>0)
               ORDER BY t.id DESC LIMIT 80""",
        )
        history_rows = all_rows(
            connection,
            """WITH execution_flow AS (
                   SELECT security_id,date_trunc('minute',created_at::timestamptz) AS minute_bucket,
                           SUM(COALESCE(buy_volume,0)) AS buy_volume,
                           SUM(COALESCE(sell_volume,0)) AS sell_volume,
                           SUM(COALESCE(buy_trade_count,0)) AS buy_trade_count,
                           SUM(COALESCE(sell_trade_count,0)) AS sell_trade_count,
                           SUM((COALESCE(buy_volume,0)+COALESCE(sell_volume,0))*COALESCE(reference_price,0)) AS traded_notional
                   FROM market_system_trades
                   WHERE created_at::timestamptz>=:since
                     AND (COALESCE(buy_volume,0)>0 OR COALESCE(sell_volume,0)>0)
                   GROUP BY security_id,date_trunc('minute',created_at::timestamptz)
               ), ranked_history AS (
                   SELECT s.ticker,h.price,h.source,h.recorded_at,
                          COALESCE(f.buy_volume,0) AS buy_volume,
                          COALESCE(f.sell_volume,0) AS sell_volume,
                          COALESCE(f.buy_trade_count,0) AS buy_trade_count,
                           COALESCE(f.sell_trade_count,0) AS sell_trade_count,
                           COALESCE(f.traded_notional,0) AS traded_notional,
                           ROW_NUMBER() OVER (PARTITION BY h.security_id ORDER BY h.id DESC) AS position
                   FROM market_price_history h
                   JOIN market_securities s ON s.id=h.security_id
                   LEFT JOIN execution_flow f ON f.security_id=h.security_id
                    AND f.minute_bucket=date_trunc('minute',h.recorded_at::timestamptz)
                   WHERE s.active=1 AND s.lifecycle_status='active'
                     AND h.recorded_at::timestamptz>=:since
                     AND (:ticker='' OR UPPER(s.ticker)=:ticker)
               )
               SELECT ticker,price,source,recorded_at,buy_volume,sell_volume,
                      buy_trade_count,sell_trade_count,traded_notional
               FROM ranked_history
               WHERE (:ticker<>'' OR position<=240)
               ORDER BY ticker,recorded_at""",
            {"since": history_since, "ticker": selected_ticker},
        )
        index_funds = all_rows(
            connection,
            """SELECT f.id,f.fund_key,f.display_name,f.risk_profile,f.target_size,
                      f.base_nav,f.management_fee_percent,f.last_rebalanced_at,
                      f.last_valued_at,s.ticker,s.price,s.previous_price,
                      (SELECT COUNT(*) FROM market_index_members m WHERE m.fund_id=f.id) AS constituent_count
               FROM market_index_funds f
               JOIN market_securities s ON s.id=f.security_id
               WHERE f.enabled=1 ORDER BY f.fund_key""",
        )
        member_rows = all_rows(
            connection,
            """SELECT m.fund_id,s.ticker,s.name,m.weight,m.rank,m.reference_price,
                      m.market_cap_at_rebalance,m.realized_volatility
               FROM market_index_members m
               JOIN market_securities s ON s.id=m.security_id
               ORDER BY m.fund_id,m.rank,s.ticker""",
        )
    price_history: dict[str, list[dict[str, Any]]] = {}
    cumulative_volume: dict[str, Decimal] = {}
    cumulative_notional: dict[str, Decimal] = {}
    for row in history_rows:
        ticker = str(row.pop("ticker")).upper()
        buy_row_volume = Decimal(str(row.pop("buy_volume", 0) or 0))
        sell_row_volume = Decimal(str(row.pop("sell_volume", 0) or 0))
        row_volume = buy_row_volume + sell_row_volume
        row_notional = Decimal(str(row.pop("traded_notional", 0) or 0))
        trade_count = int(row.pop("buy_trade_count", 0) or 0) + int(row.pop("sell_trade_count", 0) or 0)
        cumulative_volume[ticker] = cumulative_volume.get(ticker, Decimal("0")) + row_volume
        cumulative_notional[ticker] = cumulative_notional.get(ticker, Decimal("0")) + row_notional
        row["volume"] = row_volume
        row["buy_volume"] = buy_row_volume
        row["sell_volume"] = sell_row_volume
        row["trade_count"] = trade_count
        row["quote_count"] = 1
        row["bucket_vwap"] = (row_notional / row_volume) if row_volume > 0 else None
        row["cumulative_vwap"] = (
            cumulative_notional[ticker] / cumulative_volume[ticker]
            if cumulative_volume[ticker] > 0 else None
        )
        price_history.setdefault(ticker, []).append(row)
    for history_ticker, rows in list(price_history.items()):
        price_history[history_ticker] = _sample_history(rows)
    members_by_fund: dict[int, list[dict[str, Any]]] = {}
    for row in member_rows:
        fund_id = int(row.pop("fund_id"))
        members_by_fund.setdefault(fund_id, []).append(row)
    for fund in index_funds:
        fund["members"] = members_by_fund.get(int(fund["id"]), [])
    trade_tape: list[dict[str, Any]] = []
    for raw in raw_trade_tape:
        source = str(raw.get("source") or "market").lower()
        flow_type = "ai" if "gemini" in source else "market"
        for side, volume_key, count_key in (
            ("buy", "buy_volume", "buy_trade_count"),
            ("sell", "sell_volume", "sell_trade_count"),
        ):
            quantity = Decimal(str(raw.get(volume_key) or 0))
            if quantity <= 0:
                continue
            trade_tape.append({
                "id": f"{raw['id']}-{side}",
                "ticker": str(raw.get("ticker") or "FCX").upper(),
                "security_name": raw.get("security_name") or "",
                "side": side,
                "quantity": quantity,
                "unit_price": raw.get("reference_price") or 0,
                "trade_count": int(raw.get(count_key) or 0),
                "flow_type": flow_type,
                "source": raw.get("source") or "market",
                "price_change_percent": raw.get("price_change_percent") or 0,
                "created_at": raw.get("created_at"),
            })
    trade_tape = trade_tape[:80]
    buy_volume = sum((Decimal(str(row.get("buy_volume") or 0)) for row in raw_trade_tape), Decimal("0"))
    sell_volume = sum((Decimal(str(row.get("sell_volume") or 0)) for row in raw_trade_tape), Decimal("0"))
    return {
        "ok": True,
        "community_id": principal["community_id"],
        "permissions": {"trading": principal["trading_enabled"], "buy": principal["buy_enabled"], "sell": principal["sell_enabled"]},
        "market": {row["setting_key"]: row["setting_value"] for row in settings_rows},
        "history_range": normalized_range,
        "history_range_start": history_since,
        "history_range_end": history_now,
        "history_window_start": history_since,
        "selected_ticker": selected_ticker,
        "securities": securities,
        "anonymous_trade_tape": trade_tape,
        "price_history": price_history,
        "index_funds": index_funds,
        "company_wire": [],
        "market_analytics": {
            "recorded_buy_volume": buy_volume,
            "recorded_sell_volume": sell_volume,
            "recorded_trade_events": sum(int(row.get("trade_count") or 0) for row in trade_tape),
        },
    }


@router.get("/community/ravenhood/{community_user_id}/portfolio")
def community_portfolio(
    community_user_id: str,
    account_id: str,
    principal: dict[str, Any] = Depends(require_community_scopes("market:read")),
) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    with transaction() as connection:
        account = _linked_market_account(
            connection,
            community_id=community_id,
            community_user_id=community_user_id,
            account_id=account_id,
            enforce_restrictions=False,
        )
        holdings = all_rows(
            connection,
            """SELECT s.ticker,s.name,h.quantity,h.reserved_quantity,h.average_cost,s.price,
                      ROUND(h.quantity*s.price,2) AS market_value
               FROM market_holdings h JOIN market_securities s ON s.id=h.security_id
               WHERE h.account_id=:account AND h.quantity>0 ORDER BY s.ticker""",
            {"account": account["market_account_id"]},
        )
        orders = all_rows(
            connection,
            """SELECT tr.*,s.ticker,s.name AS security_name
               FROM fcx_trade_requests tr JOIN market_securities s ON s.id=tr.security_id
               WHERE tr.community_id=:community AND tr.community_user_id=:user
                 AND tr.ravenhood_account_id=:account ORDER BY tr.created_at DESC LIMIT 200""",
            {"community": community_id, "user": community_user_id, "account": account_id},
        )
    return {"ok": True, "account": account, "holdings": holdings, "orders": orders}


@router.post("/community/orders")
def create_trade_order(
    payload: TradeOrderRequest,
    principal: dict[str, Any] = Depends(require_community_scopes("trade:write", "settlement:write")),
) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    ticker = payload.ticker.upper()
    settlement_id = ""
    with transaction() as connection:
        existing = one(
            connection,
            "SELECT trade_request_id FROM fcx_trade_requests WHERE community_id=:community AND idempotency_key=:key",
            {"community": community_id, "key": payload.idempotency_key},
        )
        if existing:
            return {"ok": True, "idempotent_replay": True, "trade": _trade_response(connection, str(existing["trade_request_id"]), community_id)}
        _assert_market_access(connection, principal, payload.side)
        account = _linked_market_account(
            connection,
            community_id=community_id,
            community_user_id=payload.community_user_id,
            account_id=payload.account_id,
            lock=True,
            restriction_lane="equity",
        )
        security = _security_for_trade(connection, ticker, side=payload.side, lock=True)
        quantity = _quantity(payload.quantity)
        price = Decimal(str(security["price"]))
        gross = _money(price * quantity)
        if gross <= 0:
            raise HTTPException(status_code=400, detail="Order value must be positive")
        fee_percent = max(Decimal("0"), _setting_decimal(connection, "market_commission_percent", "4"))
        fee = _money(gross * fee_percent / Decimal("100"))
        total = gross + fee if payload.side == "buy" else max(Decimal("0.01"), gross - fee)
        trade_request_id = "fcx_ord_" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")
        if payload.side == "sell":
            holding = one(
                connection,
                "SELECT * FROM market_holdings WHERE account_id=:account AND security_id=:security FOR UPDATE",
                {"account": account["market_account_id"], "security": security["id"]},
            )
            available = _quantity((holding or {}).get("quantity", 0)) - _quantity((holding or {}).get("reserved_quantity", 0))
            if available < quantity:
                raise HTTPException(status_code=409, detail="Insufficient available shares")
            execute(connection, "UPDATE market_holdings SET reserved_quantity=reserved_quantity+:quantity WHERE id=:id", {"quantity": quantity, "id": holding["id"]})
        execute(
            connection,
            """INSERT INTO fcx_trade_requests
               (trade_request_id,idempotency_key,community_id,community_user_id,
                ravenhood_account_id,market_account_id,security_id,side,quantity,
                submitted_price,estimated_gross,estimated_fee,estimated_total,status,
                created_at,updated_at)
               VALUES (:request,:key,:community,:user,:ravenhood,:market_account,:security,
                :side,:quantity,:price,:gross,:fee,:total,'CREATED',:now,:now)""",
            {"request": trade_request_id, "key": payload.idempotency_key, "community": community_id, "user": payload.community_user_id, "ravenhood": payload.account_id, "market_account": account["market_account_id"], "security": security["id"], "side": payload.side, "quantity": quantity, "price": price, "gross": gross, "fee": fee, "total": total, "now": utcnow()},
        )
        trade = _trade_response(connection, trade_request_id, community_id)
        if payload.side == "buy":
            settlement_id = _create_trade_settlement(connection, trade=trade, operation="debit", amount=total)
            execute(connection, "UPDATE fcx_trade_requests SET status='BANK_PENDING',debit_settlement_id=:settlement,updated_at=NOW() WHERE trade_request_id=:request", {"settlement": settlement_id, "request": trade_request_id})
        audit(connection, actor_type="community", actor_id=community_id, action="trade.requested", target_type="trade_request", target_id=trade_request_id, new=payload.model_dump())
    if payload.side == "sell":
        settlement_id = _execute_sell_and_create_credit(trade_request_id, community_id)
    if not settlement_id:
        raise HTTPException(status_code=500, detail="Trade settlement was not created")
    try:
        settlement = _send_trade_settlement(community_id, settlement_id)
        if payload.side == "buy" and str(settlement["state"]) == "BANK_DEBITED":
            _execute_buy_after_debit(trade_request_id, community_id)
        if payload.side == "sell" and str(settlement["state"]) == "BANK_CREDITED":
            _finalize_sell_credit(trade_request_id, community_id)
    except HTTPException:
        with transaction() as connection:
            execute(connection, "UPDATE fcx_trade_requests SET status='BANK_RETRY_REQUIRED',updated_at=NOW() WHERE trade_request_id=:request AND status<>'SETTLED'", {"request": trade_request_id})
    with transaction() as connection:
        result = _trade_response(connection, trade_request_id, community_id)
    return {"ok": True, "idempotent_replay": False, "trade": result}


@router.get("/community/orders/{trade_request_id}")
def get_trade_order(
    trade_request_id: str,
    principal: dict[str, Any] = Depends(require_community_scopes("trade:write")),
) -> dict[str, Any]:
    with transaction() as connection:
        result = _trade_response(connection, trade_request_id, str(principal["community_id"]))
    return {"ok": True, "trade": result}


@router.post("/community/orders/{trade_request_id}/refresh")
def refresh_trade_order(
    trade_request_id: str,
    principal: dict[str, Any] = Depends(require_community_scopes("trade:write", "settlement:write")),
) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    with transaction() as connection:
        trade = _trade_response(connection, trade_request_id, community_id)
    if str(trade["status"]) != "SETTLED":
        settlement_id = str(trade.get("debit_settlement_id") or trade.get("credit_settlement_id") or "")
        if not settlement_id:
            raise HTTPException(status_code=409, detail="Trade has no bank settlement")
        settlement = _send_trade_settlement(community_id, settlement_id)
        if str(trade["side"]) == "buy" and str(settlement["state"]) == "BANK_DEBITED":
            _execute_buy_after_debit(trade_request_id, community_id)
        elif str(trade["side"]) == "sell" and str(settlement["state"]) == "BANK_CREDITED":
            _finalize_sell_credit(trade_request_id, community_id)
    with transaction() as connection:
        result = _trade_response(connection, trade_request_id, community_id)
    return {"ok": True, "trade": result}
