from __future__ import annotations

from collections.abc import Iterable


MANAGED_CONTROL_ROLES = ("commissioner", "fec_admin", "fcx_admin", "fec_investigator")

CONTROL_ROLE_CATALOG = (
    {"key": "commissioner", "label": "Commissioner", "access": "Unrestricted platform authority", "description": "Full access to every FCX and FEC workspace, control, investigation, community connection, onboarding action, and developer-level maintenance operation."},
    {"key": "fec_admin", "label": "FEC Administrator", "access": "Regulatory administration", "description": "Controls markets, indexes, leverage, promotions, investigations, banking review, and FEC Investigator onboarding."},
    {"key": "fcx_admin", "label": "FCX Administrator", "access": "Community operations", "description": "Manages CAD community connections and credentials while monitoring FCX accounts, positions, banking, audit, and health."},
    {"key": "fec_investigator", "label": "FEC Investigator", "access": "Read-only investigations", "description": "Reviews resident accounts, transaction history, P/L, leverage, banking information, diagnostics, and audit activity without market controls."},
)


def assignable_control_roles(actor_roles: Iterable[str]) -> list[str]:
    roles = {str(role) for role in actor_roles}
    if roles.intersection({"super_admin", "developer", "commissioner"}):
        return list(MANAGED_CONTROL_ROLES)
    if "fec_admin" in roles:
        return ["fec_investigator"]
    return []


def has_required_role(actor_roles: Iterable[str], allowed_roles: Iterable[str]) -> bool:
    roles = {str(role) for role in actor_roles}
    required = {str(role) for role in allowed_roles}
    return bool(roles.intersection({"super_admin", "commissioner"}) or roles.intersection(required))
