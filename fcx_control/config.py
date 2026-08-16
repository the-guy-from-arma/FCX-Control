from __future__ import annotations

import os
from dataclasses import dataclass


def required(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    cookie_secure: bool
    public_origin: str
    bootstrap_email: str
    bootstrap_password: str
    bootstrap_force_sync: bool
    bootstrap_community_id: str
    bootstrap_community_name: str
    bootstrap_community_api_key: str
    bootstrap_community_bank_bridge_url: str
    bootstrap_community_bank_secret_env: str
    bootstrap_cad2_community_id: str
    bootstrap_cad2_community_name: str
    bootstrap_cad2_community_api_key: str
    bootstrap_cad2_community_bank_bridge_url: str
    bootstrap_cad2_community_bank_secret_env: str
    api_rate_limit_per_minute: int

    @classmethod
    def load(cls) -> "Settings":
        url = required("FCX_DATABASE_URL")
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://") and "+psycopg" not in url:
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return cls(
            database_url=url,
            session_secret=required("FCX_SESSION_SECRET"),
            cookie_secure=flag("FCX_COOKIE_SECURE", True),
            public_origin=str(os.environ.get("FCX_PUBLIC_ORIGIN") or "").rstrip("/"),
            bootstrap_email=str(os.environ.get("FCX_BOOTSTRAP_ADMIN_EMAIL") or os.environ.get("OWNER_EMAIL") or "").strip().lower(),
            bootstrap_password=str(os.environ.get("FCX_BOOTSTRAP_ADMIN_PASSWORD") or os.environ.get("OWNER_PASSWORD") or ""),
            bootstrap_force_sync=flag("FCX_BOOTSTRAP_FORCE_SYNC", False),
            bootstrap_community_id=str(os.environ.get("FCX_BOOTSTRAP_COMMUNITY_ID") or "").strip().lower(),
            bootstrap_community_name=str(os.environ.get("FCX_BOOTSTRAP_COMMUNITY_NAME") or "").strip(),
            bootstrap_community_api_key=str(os.environ.get("FCX_BOOTSTRAP_COMMUNITY_API_KEY") or "").strip(),
            bootstrap_community_bank_bridge_url=str(os.environ.get("FCX_BOOTSTRAP_BANK_BRIDGE_URL") or "").strip().rstrip("/"),
            bootstrap_community_bank_secret_env=str(os.environ.get("FCX_BOOTSTRAP_BANK_SECRET_ENV") or "").strip(),
            bootstrap_cad2_community_id=str(os.environ.get("FCX_BOOTSTRAP_CAD2_COMMUNITY_ID") or "").strip().lower(),
            bootstrap_cad2_community_name=str(os.environ.get("FCX_BOOTSTRAP_CAD2_COMMUNITY_NAME") or "").strip(),
            bootstrap_cad2_community_api_key=str(os.environ.get("FCX_BOOTSTRAP_CAD2_COMMUNITY_API_KEY") or "").strip(),
            bootstrap_cad2_community_bank_bridge_url=str(os.environ.get("FCX_BOOTSTRAP_CAD2_BANK_BRIDGE_URL") or "").strip().rstrip("/"),
            bootstrap_cad2_community_bank_secret_env=str(os.environ.get("FCX_BOOTSTRAP_CAD2_BANK_SECRET_ENV") or "").strip(),
            api_rate_limit_per_minute=max(10, int(os.environ.get("FCX_API_RATE_LIMIT_PER_MINUTE", "120"))),
        )


settings = Settings.load()
