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
    status: str = "pending"


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
    if account.role not in {"member", "admin"} or account.status not in {"pending", "approved", "rejected"}:
        raise AccessDenied("invalid_account_state")
    return Actor(principal, account)


def require_permission(actor: Actor, permission: str) -> None:
    if actor.account.status == "disabled":
        raise AccessDenied("account_disabled")
    if actor.account.role not in {"member", "admin"} or actor.account.status not in {"pending", "approved", "rejected"}:
        raise AccessDenied("invalid_account_state")
    if permission not in actor.principal.scopes:
        raise AccessDenied("permission_denied")
    permissions = SELF_PERMISSIONS
    if actor.account.status == "approved":
        permissions |= APPROVED_PERMISSIONS
        if actor.account.role == "admin":
            permissions |= ADMIN_PERMISSIONS
    if permission not in permissions:
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
