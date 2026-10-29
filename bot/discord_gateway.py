from __future__ import annotations
import logging
from typing import Sequence
import discord
from bot.sync import MemberSnapshot, RoleInfo, RoleOperationError

logger = logging.getLogger("bot.gateway")

def snapshot_from_member(member: discord.Member) -> MemberSnapshot:
    
    return MemberSnapshot(id=member.id,username=member.name,role_names=frozenset(r.name for r in member.roles if not r.is_default()),)


class DiscordGuildGateway:

    def __init__(self, guild: discord.Guild) -> None:
        self._guild = guild

    @property
    def guild_id(self) -> int:
        return self._guild.id

    @property
    def bot_can_manage_roles(self) -> bool:
        me = self._guild.me
        return bool(me is not None and me.guild_permissions.manage_roles)

    async def getMembers(self) -> Sequence[MemberSnapshot]:
        
        members: list[MemberSnapshot] = []
        async for member in self._guild.fetch_members(limit=None):
            members.append(snapshot_from_member(member))
        logger.info("Loaded %d members from guild %s", len(members), self._guild.id)
        return members

    def listRoles(self) -> Sequence[RoleInfo]:
        me = self._guild.me
        infos: list[RoleInfo] = []
        for role in self._guild.roles:
            if role.is_default():
                continue
            problem: str | None = None
            if role.managed:
                problem = "role is managed by an integration and cannot be assigned manually"
            elif me is None:
                problem = "the bot's own member could not be determined"
            elif not me.guild_permissions.manage_roles:
                problem = "the bot lacks the Manage Roles permission"
            elif role >= me.top_role:
                problem = "role is not below the bot's highest role (move the bot's role higher)"
            infos.append(RoleInfo(name=role.name, id=role.id, problem=problem))
        return infos

    async def addRole(self, member_id: int, role_name: str, reason: str) -> None:
        member, role = await self._resolve(member_id, role_name)
        await self._call(member.add_roles(role, reason=reason[:480]), role_name)

    async def removeRole(self, member_id: int, role_name: str, reason: str) -> None:
        member, role = await self._resolve(member_id, role_name)
        await self._call(member.remove_roles(role, reason=reason[:480]), role_name)

    async def _resolve(self, member_id: int, role_name: str) -> tuple[discord.Member, discord.Role]:
        role = discord.utils.get(self._guild.roles, name=role_name)
        if role is None:
            raise RoleOperationError(f"Role '{role_name}' no longer exists in the server")
        member = self._guild.get_member(member_id)
        if member is None:
            try:
                member = await self._guild.fetch_member(member_id)
            except discord.NotFound as exc:
                raise RoleOperationError("Member is no longer in the server") from exc
            except discord.HTTPException as exc:
                raise RoleOperationError(f"Could not fetch member (Discord error {exc.status})") from exc
        return member, role

    @staticmethod
    async def _call(awaitable, role_name: str) -> None:
        try:
            await awaitable
        except discord.Forbidden as exc:
            raise RoleOperationError(
                "Discord refused: missing permission or role hierarchy "
                f"(is the bot's role above '{role_name}' and the member's top role?)"
            ) from exc
        except discord.NotFound as exc:
            raise RoleOperationError("Member or role no longer exists") from exc
        except discord.HTTPException as exc:
            raise RoleOperationError(f"Discord API error (HTTP {exc.status})") from exc
