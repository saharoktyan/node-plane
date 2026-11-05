"""Resource authorization for already authenticated principals.

Only credential verification may construct a Principal from transport input.
Roles and account status are loaded from the repository on every invocation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol


class AccessDenied(Exception):
    def __init__(self, code: str, status: int = 403):
        super().__init__(code)
        self.code = code
        self.status = status


class PrincipalKind(str, Enum):
    ACCOUNT = "account"
    ADAPTER = "adapter"
    SERVICE = "service"


@dataclass(frozen=True)
class Principal:
    id: str
    kind: PrincipalKind
    scopes: frozenset[str]
    account_id: str | None = None


@dataclass(frozen=True)
class Account:
    id: str
    role: str = "member"
    # An account is active (`approved`) or `disabled`; access to services
    # belongs to profiles, and visitors have no account at all.
    status: str = "approved"
    # Assigned roles for the whole installation; None when not loaded, then
    # `role` decides (an administrator without assignments is an owner).
    roles: frozenset[str] | None = None


@dataclass(frozen=True)
class Actor:
    principal: Principal
    account: Account


@dataclass(frozen=True)
class ProfileResource:
    id: str
    owner_account_id: str | None
    frozen: bool = False
    expires_at: datetime | None = None


class IdentityReader(Protocol):
    def get_account(self, account_id: str) -> Account | None: ...
    def find_account(self, provider: str, subject: str) -> Account | None: ...
    def find_telegram_account(self, user_id: int) -> Account | None: ...


SELF_PERMISSIONS = frozenset({
    "account.self.read", "account.self.preferences.write",
    "access_requests.self.create", "access_requests.self.read",
})
APPROVED_PERMISSIONS = frozenset({
    "profiles.self.read", "nodes.available.read", "configs.self.issue", "configs.self.read",
    "operations.read",
})
ADMIN_PERMISSIONS = frozenset({
    "access_requests.manage", "accounts.manage", "profiles.manage", "grants.manage",
    "configs.manage.issue", "configs.manage.read", "nodes.manage", "nodes.inspect",
    "nodes.execute", "maintenance.manage", "settings.manage", "diagnostics.sensitive.read",
})


# Role presets. Every active account has the member's own permissions; the
# roles add to them. Custom roles would be further rows of `backend_roles`.
MEMBER_PERMISSIONS = SELF_PERMISSIONS | APPROVED_PERMISSIONS
ROLE_PERMISSIONS = frozenset({"roles.manage"})


def service_permission(service: str) -> str:
    """Managing one type of service on nodes: its instances, settings and
    their application. Clients ask for it with the `nodes.manage` scope;
    the account's roles decide which types."""
    return f"services.{service}.manage"


from db.migration_revisions.r0033_service_instances import TYPES as _SERVICE_TYPES  # noqa: E402
SERVICE_PERMISSIONS = frozenset(service_permission(service) for service in _SERVICE_TYPES)
PRESETS = {
    "owner": ADMIN_PERMISSIONS | ROLE_PERMISSIONS | SERVICE_PERMISSIONS,
    "admin": ADMIN_PERMISSIONS | SERVICE_PERMISSIONS,
    "operator": frozenset({"nodes.manage", "nodes.inspect", "nodes.execute", "maintenance.manage",
                           "diagnostics.sensitive.read"}) | SERVICE_PERMISSIONS,
    "support": frozenset({"access_requests.manage", "profiles.manage", "configs.manage.read",
                          "configs.manage.issue"}),
}
# Roles that make an account staff; `backend_accounts.role` mirrors them
# as `admin`.
STAFF_ROLES = frozenset(PRESETS)


def effective_roles(account: Account) -> frozenset[str]:
    roles = getattr(account, "roles", None)
    if roles is not None:
        return roles
    return frozenset({"owner"}) if account.role == "admin" else frozenset()


def account_permissions(account: Account) -> frozenset[str]:
    """What an active account may do: its own things plus its roles."""
    permissions = MEMBER_PERMISSIONS
    for role in effective_roles(account):
        permissions |= PRESETS.get(role, frozenset())
    return permissions


def validate_telegram_id(user_id: int) -> None:
    if type(user_id) is not int or not 0 < user_id < 2**63:
        raise AccessDenied("invalid_telegram_identity", 422)


def resolve_actor(principal: Principal, identities: IdentityReader, *, telegram_user_id: int | None = None) -> Actor:
    if principal.kind == PrincipalKind.ACCOUNT:
        if telegram_user_id is not None:
            raise AccessDenied("delegation_not_allowed")
        account = identities.get_account(principal.account_id or "")
    elif principal.kind == PrincipalKind.ADAPTER:
        if "delegate.telegram" not in principal.scopes:
            raise AccessDenied("delegation_not_allowed")
        validate_telegram_id(telegram_user_id)
        account = identities.find_telegram_account(telegram_user_id)
    else:
        # Background service authorization will have a separate explicit actor.
        raise AccessDenied("human_actor_required")
    if account is None:
        raise AccessDenied("identity_not_registered", 401)
    if account.status == "disabled":
        raise AccessDenied("account_disabled")
    if account.role not in {"member", "admin"} or account.status != "approved":
        raise AccessDenied("invalid_account_state")
    return Actor(principal, account)


def require_permission(actor: Actor, permission: str) -> None:
    if actor.account.status == "disabled":
        raise AccessDenied("account_disabled")
    if actor.account.role not in {"member", "admin"} or actor.account.status != "approved":
        raise AccessDenied("invalid_account_state")
    scope = "nodes.manage" if permission.startswith("services.") else permission
    if scope not in actor.principal.scopes:
        raise AccessDenied("permission_denied")
    if permission not in account_permissions(actor.account):
        raise AccessDenied("permission_denied")


def require_profile(actor: Actor, profile: ProfileResource, *, action: str = "read", administrative: bool = False,
                    grant_active: bool = False, now: datetime | None = None) -> None:
    if action not in {"read", "issue_config", "read_config"}:
        raise ValueError("unsupported profile action")
    own = profile.owner_account_id == actor.account.id
    if not administrative and not own:
        raise AccessDenied("resource_not_found", 404)
    permission = {
        (False, "read"): "profiles.self.read", (True, "read"): "profiles.manage",
        (False, "issue_config"): "configs.self.issue", (True, "issue_config"): "configs.manage.issue",
        (False, "read_config"): "configs.self.read", (True, "read_config"): "configs.manage.read",
    }[administrative, action]
    require_permission(actor, permission)
    if action == "read":
        return
    if not grant_active:
        raise AccessDenied("grant_revoked")
    if profile.frozen:
        raise AccessDenied("profile_frozen")
    if profile.expires_at is not None:
        if profile.expires_at.tzinfo is None:
            raise AccessDenied("invalid_profile_state")
        if profile.expires_at <= (now or datetime.now(timezone.utc)):
            raise AccessDenied("profile_expired")
