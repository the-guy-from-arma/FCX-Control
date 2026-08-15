from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import settings
from .db import all_rows, execute, one, transaction
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
    with transaction() as connection:
        user = one(
            connection,
            "SELECT * FROM fcx_control_admin_users WHERE email=:email AND active=TRUE LIMIT 1",
            {"email": payload.email.strip().lower()},
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
def overview(_: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES))) -> dict[str, Any]:
    with transaction() as connection:
        communities = all_rows(connection, "SELECT * FROM fcx_communities ORDER BY community_name")
        totals = one(
            connection,
            """SELECT
              (SELECT COUNT(*) FROM fcx_ravenhood_accounts WHERE status='active') AS active_accounts,
              (SELECT COUNT(*) FROM fcx_settlements) AS settlements,
              (SELECT COALESCE(SUM(amount),0) FROM fcx_settlements WHERE state='SETTLED') AS settled_value,
              (SELECT COUNT(*) FROM fcx_investigations WHERE status='open') AS open_cases,
              (SELECT COUNT(*) FROM market_securities WHERE active=1) AS securities,
              (SELECT COALESCE(SUM(price*issued_shares),0) FROM market_securities WHERE active=1) AS market_cap""",
        ) or {}
        recent = all_rows(connection, "SELECT * FROM fcx_audit_log ORDER BY id DESC LIMIT 25")
    return {"ok": True, "totals": totals, "communities": communities, "recent_actions": recent}


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
def admin_settlements(state: str = "", community_id: str = "", _: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES))) -> dict[str, Any]:
    clauses: list[str] = []
    values: dict[str, Any] = {}
    if state:
        clauses.append("state=:state")
        values["state"] = state.upper()
    if community_id:
        clauses.append("community_id=:community")
        values["community"] = community_id
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with transaction() as connection:
        rows = all_rows(connection, f"SELECT * FROM fcx_settlements{where} ORDER BY id DESC LIMIT 500", values)
    return {"ok": True, "settlements": rows}


@router.get("/admin/audit")
def audit_log(limit: int = 200, _: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES))) -> dict[str, Any]:
    limit = max(1, min(1000, limit))
    with transaction() as connection:
        rows = all_rows(connection, "SELECT * FROM fcx_audit_log ORDER BY id DESC LIMIT :limit", {"limit": limit})
    return {"ok": True, "audit": rows}


@router.get("/admin/accounts")
def accounts(limit: int = 200, _: dict[str, Any] = Depends(require_roles(*ADMIN_ROLES))) -> dict[str, Any]:
    safe_limit = min(max(limit, 1), 1000)
    with transaction() as connection:
        rows = all_rows(
            connection,
            """SELECT r.id,r.account_id,r.display_name,r.status,r.created_at,r.updated_at,
                      a.id AS market_account_id,a.cash_balance,a.status AS trading_status,
                      COUNT(DISTINCT l.community_id) AS community_count,
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


@router.get("/admin/investigations")
def investigations(limit: int = 200, _: dict[str, Any] = Depends(require_roles("developer"))) -> dict[str, Any]:
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


@router.patch("/admin/investigations/{case_id}")
def update_investigation(case_id: int, payload: InvestigationPatch, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer"))) -> dict[str, Any]:
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
        securities = all_rows(connection, "SELECT * FROM market_securities ORDER BY active DESC,ticker")
        settings_rows = all_rows(connection, "SELECT setting_key,setting_value,updated_at FROM system_settings ORDER BY setting_key")
        order_counts = all_rows(connection, "SELECT status,COUNT(*) AS count FROM market_order_requests GROUP BY status ORDER BY status")
        recent_orders = all_rows(connection, """SELECT o.*,s.ticker,r.display_name
            FROM market_orders o JOIN market_securities s ON s.id=o.security_id
            JOIN market_accounts a ON a.id=o.account_id JOIN fcx_ravenhood_accounts r ON r.id=a.user_id
            ORDER BY o.id DESC LIMIT 100""")
    return {"ok": True, "securities": securities, "settings": settings_rows, "order_counts": order_counts, "recent_orders": recent_orders}


@router.patch("/admin/market/settings")
def update_market_settings(payload: MarketSettingsPatch, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer"))) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No market settings supplied")
    with transaction() as connection:
        previous_rows = all_rows(connection, "SELECT setting_key,setting_value FROM system_settings")
        previous = {row["setting_key"]: row["setting_value"] for row in previous_rows if row["setting_key"] in changes}
        for key, value in changes.items():
            stored = "1" if value is True else "0" if value is False else str(value)
            execute(connection, """INSERT INTO system_settings(setting_key,setting_value,updated_at) VALUES (:key,:value,NOW())
                ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at""", {"key": key, "value": stored})
        audit(connection, actor_type="admin", actor_id=str(user["id"]), actor_role=",".join(user["roles"]), action="market.settings.updated", target_type="market", previous=previous, new=changes, request=request)
    return {"ok": True, "settings": changes}


@router.get("/admin/credentials")
def credentials(_: dict[str, Any] = Depends(require_roles("fcx_admin", "commissioner"))) -> dict[str, Any]:
    with transaction() as connection:
        rows = all_rows(connection, """SELECT credential_id,community_id,scopes_json,active,last_used_at,created_at,revoked_at
            FROM fcx_community_credentials ORDER BY created_at DESC""")
    return {"ok": True, "credentials": rows}


@router.post("/admin/investigations")
def open_investigation(payload: InvestigationRequest, request: Request, user: dict[str, Any] = Depends(require_admin_csrf("developer"))) -> dict[str, Any]:
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
        current = _settlement_response(connection, settlement_id, community_id)
        if str(current["state"]) in {"SETTLED", "REVERSED"}:
            return current
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
    if payload.operation == "debit" and (not principal["trading_enabled"] or not principal["buy_enabled"]):
        raise HTTPException(status_code=403, detail="Buying is disabled for this community")
    if payload.operation == "credit" and (not principal["trading_enabled"] or not principal["sell_enabled"]):
        raise HTTPException(status_code=403, detail="Selling is disabled for this community")
    with transaction() as connection:
        existing = one(connection, "SELECT * FROM fcx_settlements WHERE community_id=:community AND idempotency_key=:key", {"community": community_id, "key": payload.idempotency_key})
        if existing:
            return {"ok": True, "idempotent_replay": True, "settlement": existing}
        link = one(connection, "SELECT id FROM fcx_ravenhood_links WHERE account_id=:account AND community_id=:community AND community_user_id=:user", {"account": payload.account_id, "community": community_id, "user": payload.community_user_id})
        if not link:
            raise HTTPException(status_code=403, detail="Ravenhood account is not linked to this community user")
        settlement_id = "fcx_txn_" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")
        execute(
            connection,
            """INSERT INTO fcx_settlements
               (settlement_id,idempotency_key,community_id,account_id,community_user_id,
                operation,amount,currency,state,order_reference,request_json,created_at,updated_at)
               VALUES (:settlement,:key,:community,:account,:user,:operation,:amount,:currency,
                'CREATED',:order_reference,CAST(:request AS jsonb),:now,:now)""",
            {"settlement": settlement_id, "key": payload.idempotency_key, "community": community_id, "account": payload.account_id, "user": payload.community_user_id, "operation": payload.operation, "amount": payload.amount, "currency": payload.currency, "order_reference": payload.order_reference, "request": json.dumps(payload.metadata, default=str), "now": utcnow()},
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
            execute(connection, "UPDATE fcx_settlements SET state='FAILED',failure_code='BANK_BRIDGE_UNAVAILABLE',failure_message=:message,updated_at=NOW() WHERE settlement_id=:settlement AND state IN ('CREATED','FAILED')", {"message": str(exc)[:2000], "settlement": settlement_id})
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
        execute(
            connection,
            """UPDATE fcx_settlements SET state=:state,bank_reference=:reference,
               failure_code=:failure_code,failure_message=:failure_message,
               response_json=CAST(:response AS jsonb),updated_at=NOW()
               WHERE settlement_id=:settlement AND community_id=:community""",
            {"state": payload.state, "reference": payload.bank_reference, "failure_code": payload.failure_code, "failure_message": payload.failure_message, "response": json.dumps(payload.response, default=str), "settlement": settlement_id, "community": community_id},
        )
        audit(connection, actor_type="community", actor_id=community_id, action="settlement.state_changed", target_type="settlement", target_id=settlement_id, previous={"state": current["state"]}, new=payload.model_dump())
        updated = _settlement_response(connection, settlement_id, community_id)
    return {"ok": True, "idempotent_replay": False, "settlement": updated}


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


def _linked_market_account(
    connection: Any,
    *,
    community_id: str,
    community_user_id: str,
    account_id: str,
    lock: bool = False,
) -> dict[str, Any]:
    suffix = " FOR UPDATE OF ma" if lock else ""
    row = one(
        connection,
        """SELECT ra.id AS ravenhood_internal_id,ra.account_id,ra.display_name,
                  ra.status AS ravenhood_status,rl.verified,rl.active AS link_active,
                  ma.id AS market_account_id,ma.status AS market_status
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
    if str(row.get("ravenhood_status")) != "active" or str(row.get("market_status")) != "active":
        raise HTTPException(status_code=403, detail="Ravenhood trading account is restricted")
    restriction = one(
        connection,
        """SELECT id,scope,reason FROM market_account_trading_restrictions
           WHERE account_id=:account AND status='active' LIMIT 1""",
        {"account": row["market_account_id"]},
    )
    if restriction:
        raise HTTPException(status_code=403, detail="Ravenhood trading account is restricted by FEC")
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


@router.get("/community/market")
def community_market(principal: dict[str, Any] = Depends(require_community_scopes("market:read"))) -> dict[str, Any]:
    with transaction() as connection:
        securities = all_rows(
            connection,
            """SELECT s.id,s.ticker,s.name,s.security_type,s.sector,s.description,s.price,
                      s.previous_price,s.volatility,s.updated_at,
                      EXISTS(SELECT 1 FROM market_security_halts h WHERE h.security_id=s.id AND h.status='active') AS halted
               FROM market_securities s
               WHERE s.active=1 AND s.lifecycle_status='active'
                 AND NOT EXISTS(SELECT 1 FROM market_security_delistings d WHERE d.security_id=s.id AND d.status='active')
               ORDER BY s.ticker""",
        )
        settings_rows = all_rows(connection, "SELECT setting_key,setting_value FROM system_settings WHERE setting_key IN ('market_open','buy_enabled','sell_enabled','maintenance_mode')")
    return {
        "ok": True,
        "community_id": principal["community_id"],
        "permissions": {"trading": principal["trading_enabled"], "buy": principal["buy_enabled"], "sell": principal["sell_enabled"]},
        "market": {row["setting_key"]: row["setting_value"] for row in settings_rows},
        "securities": securities,
    }


@router.get("/community/ravenhood/{community_user_id}/portfolio")
def community_portfolio(
    community_user_id: str,
    account_id: str,
    principal: dict[str, Any] = Depends(require_community_scopes("market:read")),
) -> dict[str, Any]:
    community_id = str(principal["community_id"])
    with transaction() as connection:
        account = _linked_market_account(connection, community_id=community_id, community_user_id=community_user_id, account_id=account_id)
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
        account = _linked_market_account(connection, community_id=community_id, community_user_id=payload.community_user_id, account_id=payload.account_id, lock=True)
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
