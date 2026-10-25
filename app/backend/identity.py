"""Identity registration and current actor queries; no Telegram dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from .telegram_visitors import TelegramVisitor, observe

from .authorization import (
    Account, Principal, require_permission, resolve_actor,
)


class IdentityRepository(Protocol):
    db: object
    def get_account(self, account_id: str) -> Account | None: ...
    def find_telegram_account(self, user_id: int) -> Account | None: ...
    def resolve_telegram(self, user_id: int) -> Account: ...
    def resolve_telegram_command(self, principal_id: str, command_key: str, user_id: int) -> Account: ...


@dataclass(frozen=True)
class IdentityService:
    repository: IdentityRepository

    def resolve_telegram(self, principal: Principal, user_id: int, *, command_key: str | None = None,
                         username=None, first_name=None, last_name=None, language_code=None) -> Account | TelegramVisitor:
        return observe(self.repository.db, principal, user_id,
            command_key or str(uuid4()), username=username, first_name=first_name,
            last_name=last_name, language_code=language_code)

    def me(self, principal: Principal, *, telegram_user_id: int | None = None) -> Account:
        actor = resolve_actor(principal, self.repository, telegram_user_id=telegram_user_id)
        require_permission(actor, "account.self.read")
        return actor.account
