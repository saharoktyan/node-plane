"""Identity registration and current actor queries; no Telegram dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .authorization import (
    Account, AccessDenied, Principal, PrincipalKind, require_permission, resolve_actor,
    validate_telegram_id,
)


class IdentityRepository(Protocol):
    def get_account(self, account_id: str) -> Account | None: ...
    def find_telegram_account(self, user_id: int) -> Account | None: ...
    def resolve_telegram(self, user_id: int) -> Account: ...
    def resolve_telegram_command(self, principal_id: str, command_key: str, user_id: int) -> Account: ...


@dataclass(frozen=True)
class IdentityService:
    repository: IdentityRepository

    def resolve_telegram(self, principal: Principal, user_id: int, *, command_key: str | None = None) -> Account:
        if principal.kind != PrincipalKind.ADAPTER or "identity.telegram.resolve" not in principal.scopes:
            raise AccessDenied("permission_denied")
        validate_telegram_id(user_id)
        # Registration never accepts role, account ID or profile ownership.
        if command_key is not None:
            return self.repository.resolve_telegram_command(principal.id, command_key, user_id)
        return self.repository.resolve_telegram(user_id)

    def me(self, principal: Principal, *, telegram_user_id: int | None = None) -> Account:
        actor = resolve_actor(principal, self.repository, telegram_user_id=telegram_user_id)
        require_permission(actor, "account.self.read")
        return actor.account
