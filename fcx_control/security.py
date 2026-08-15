from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import Header, HTTPException, Request

from .config import settings
from .db import execute, one, transaction


PBKDF2_ROUNDS = 310_000
rate_guard = threading.Lock()
rate_windows: dict[str, deque[float]] = defaultdict(deque)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_raw, digest_raw = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_raw + "=" * (-len(salt_raw) % 4))
        expected = base64.urlsafe_b64decode(digest_raw + "=" * (-len(digest_raw) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def protected_hash(value: str) -> str:
    return hmac.new(settings.session_secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def new_api_credential() -> tuple[str, str, str]:
    credential_id = "fcx_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")
    secret = secrets.token_urlsafe(40)
    return credential_id, f"{credential_id}.{secret}", protected_hash(secret)


def create_session(user_id: int, request: Request) -> tuple[str, str, datetime]:
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=12)
    with transaction() as connection:
        execute(
            connection,
            """INSERT INTO fcx_control_admin_sessions
               (user_id,token_hash,csrf_hash,expires_at,ip_address,user_agent,created_at)
               VALUES (:user_id,:token_hash,:csrf_hash,:expires,:ip,:agent,:now)""",
            {
                "user_id": user_id,
                "token_hash": protected_hash(token),
                "csrf_hash": protected_hash(csrf),
                "expires": expires,
                "ip": request.client.host if request.client else "",
                "agent": request.headers.get("user-agent", "")[:1000],
                "now": datetime.now(timezone.utc),
            },
        )
    return token, csrf, expires


def _roles(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        value = json.loads(str(raw or "[]"))
        return [str(item) for item in value] if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def current_admin(request: Request) -> dict[str, Any]:
    token = request.cookies.get("fcx_session", "")
    if not token:
        raise HTTPException(status_code=401, detail="Administrator login required")
    with transaction() as connection:
        row = one(
            connection,
            """SELECT u.id,u.email,u.display_name,u.roles_json,u.active,
                      s.id AS session_id,s.csrf_hash,s.expires_at
               FROM fcx_control_admin_sessions s
               JOIN fcx_control_admin_users u ON u.id=s.user_id
               WHERE s.token_hash=:token_hash AND s.revoked_at IS NULL
                 AND s.expires_at>NOW() AND u.active=TRUE LIMIT 1""",
            {"token_hash": protected_hash(token)},
        )
    if not row:
        raise HTTPException(status_code=401, detail="Session expired")
    row["roles"] = _roles(row.pop("roles_json", []))
    return row


def require_roles(*allowed: str) -> Callable[[Request], dict[str, Any]]:
    required = set(allowed)

    def dependency(request: Request) -> dict[str, Any]:
        user = current_admin(request)
        if "super_admin" not in user["roles"] and not required.intersection(user["roles"]):
            raise HTTPException(status_code=403, detail="FCX role required")
        return user

    return dependency


def require_csrf(request: Request, x_fcx_csrf: str = Header(default="")) -> dict[str, Any]:
    user = current_admin(request)
    if not x_fcx_csrf or not hmac.compare_digest(protected_hash(x_fcx_csrf), str(user["csrf_hash"])):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    return user


def require_admin_csrf(*allowed: str) -> Callable[[Request, str], dict[str, Any]]:
    required = set(allowed)

    def dependency(request: Request, x_fcx_csrf: str = Header(default="")) -> dict[str, Any]:
        user = require_csrf(request, x_fcx_csrf)
        if "super_admin" not in user["roles"] and not required.intersection(user["roles"]):
            raise HTTPException(status_code=403, detail="FCX role required")
        return user

    return dependency


def _check_rate(identifier: str) -> None:
    now = time.monotonic()
    cutoff = now - 60.0
    with rate_guard:
        window = rate_windows[identifier]
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= settings.api_rate_limit_per_minute:
            raise HTTPException(status_code=429, detail="Community API rate limit exceeded")
        window.append(now)


def community_principal(
    authorization: str = Header(default=""),
    x_fcx_community: str = Header(default=""),
) -> dict[str, Any]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="FCX community credential required")
    presented = authorization[7:].strip()
    if "." not in presented:
        raise HTTPException(status_code=401, detail="Malformed FCX credential")
    credential_id, secret = presented.split(".", 1)
    _check_rate(credential_id)
    with transaction() as connection:
        row = one(
            connection,
            """SELECT c.credential_id,c.secret_hash,c.scopes_json,c.community_id,
                      m.community_name,m.status,m.connection_enabled,m.trading_enabled,
                      m.buy_enabled,m.sell_enabled,m.account_creation_enabled,m.suspended
               FROM fcx_community_credentials c
               JOIN fcx_communities m ON m.community_id=c.community_id
               WHERE c.credential_id=:credential_id AND c.active=TRUE
                 AND c.revoked_at IS NULL LIMIT 1""",
            {"credential_id": credential_id},
        )
        if not row or not hmac.compare_digest(str(row["secret_hash"]), protected_hash(secret)):
            raise HTTPException(status_code=401, detail="Invalid FCX community credential")
        if not x_fcx_community or not hmac.compare_digest(
            str(row["community_id"]).lower(), x_fcx_community.strip().lower()
        ):
            raise HTTPException(status_code=403, detail="FCX community header does not match credential")
        if not row["connection_enabled"] or row["suspended"] or row["status"] != "active":
            raise HTTPException(status_code=403, detail="Community FCX connection is disabled")
        execute(
            connection,
            "UPDATE fcx_community_credentials SET last_used_at=NOW() WHERE credential_id=:credential_id",
            {"credential_id": credential_id},
        )
        execute(
            connection,
            "UPDATE fcx_communities SET last_seen_at=NOW() WHERE community_id=:community_id",
            {"community_id": row["community_id"]},
        )
    row["scopes"] = _roles(row.pop("scopes_json", []))
    row.pop("secret_hash", None)
    return row


def require_community_scopes(*allowed: str) -> Callable[..., dict[str, Any]]:
    required = set(allowed)

    def dependency(
        authorization: str = Header(default=""),
        x_fcx_community: str = Header(default=""),
    ) -> dict[str, Any]:
        principal = community_principal(authorization, x_fcx_community)
        scopes = set(principal.get("scopes") or [])
        if "*" not in scopes and not required.issubset(scopes):
            raise HTTPException(status_code=403, detail="FCX credential scope required")
        return principal

    return dependency
