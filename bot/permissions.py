from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection

UNAUTHORIZED_MESSAGE = (
    "You do not have permission to use this command.\n"
    "Please contact an HR member or server administrator."
)
GUILD_ONLY_MESSAGE = "This command can only be used inside the configured server."


def is_authorized(member: Any, hr_role_ids: Collection[int]) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    if permissions is not None and getattr(permissions, "administrator", False) is True:
        return True

    allowed = {int(i) for i in hr_role_ids}
    if not allowed:
        return False
    roles = getattr(member, "roles", None) or []
    return any(getattr(role, "id", None) in allowed for role in roles)


@dataclass(frozen=True)
class AuthResult:
    allowed: bool
    message: str = ""


def check_interaction(interaction: Any, guild_id: int, hr_role_ids: Collection[int]) -> AuthResult:
    guild = getattr(interaction, "guild", None)
    if guild is None or getattr(guild, "id", None) != guild_id:
        return AuthResult(False, GUILD_ONLY_MESSAGE)
    if not is_authorized(getattr(interaction, "user", None), hr_role_ids):
        return AuthResult(False, UNAUTHORIZED_MESSAGE)
    return AuthResult(True)
