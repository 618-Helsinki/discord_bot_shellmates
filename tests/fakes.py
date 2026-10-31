from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Sequence
from unittest.mock import AsyncMock, MagicMock

from bot.sheets import SheetData
from bot.sync import MemberSnapshot, RoleInfo, RoleOperationError


def run(coro):
    return asyncio.run(coro)


def member(uid: int, username: str, *roles: str) -> MemberSnapshot:
    return MemberSnapshot(id=uid, username=username, role_names=frozenset(roles))


def role_infos(*names: str, blocked: dict[str, str] | None = None) -> list[RoleInfo]:
    blocked = blocked or {}
    return [RoleInfo(name=n, id=1000 + i, problem=blocked.get(n)) for i, n in enumerate(names)]


class FakeSheetSource:

    def __init__(self, data: SheetData | None = None, error: Exception | None = None) -> None:
        self._data = data
        self._error = error
        self.calls = 0

    async def fetch_sheet_data(self) -> SheetData:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._data is not None
        return self._data


class SlowSheetSource(FakeSheetSource):

    def __init__(self, data: SheetData) -> None:
        super().__init__(data)
        self.release: asyncio.Event | None = None

    async def fetch_sheet_data(self) -> SheetData:
        if self.release is None:
            self.release = asyncio.Event()
        self.started = True
        await self.release.wait()
        return await super().fetch_sheet_data()


class FakeGateway:

    def __init__(
        self,
        members: Sequence[MemberSnapshot],
        roles: Sequence[RoleInfo],
        *,
        can_manage_roles: bool = True,
        fail: dict[tuple[int, str, str], str] | None = None,
        members_error: Exception | None = None,
        guild_id: int = 1,
    ) -> None:
        self.guild_id = guild_id
        self._members = list(members)
        self._roles = list(roles)
        self._can_manage = can_manage_roles
        self._fail = fail or {}
        self._members_error = members_error
        self.calls: list[tuple[str, int, str]] = []

    @property
    def bot_can_manage_roles(self) -> bool:
        return self._can_manage

    async def getMembers(self):
        if self._members_error:
            raise self._members_error
        return list(self._members)

    def listRoles(self):
        return list(self._roles)

    async def addRole(self, member_id: int, role_name: str, reason: str) -> None:
        self.calls.append(("add", member_id, role_name))
        message = self._fail.get((member_id, role_name, "add"))
        if message:
            raise RoleOperationError(message)

    async def removeRole(self, member_id: int, role_name: str, reason: str) -> None:
        self.calls.append(("remove", member_id, role_name))
        message = self._fail.get((member_id, role_name, "remove"))
        if message:
            raise RoleOperationError(message)


def fake_interaction(*, guild_id: int = 1, admin: bool = False, role_ids: Sequence[int] = (), in_guild: bool = True):
    user = SimpleNamespace(
        id=42,
        name="hr_person",
        guild_permissions=SimpleNamespace(administrator=admin),
        roles=[SimpleNamespace(id=i) for i in role_ids],
    )
    interaction = MagicMock()
    interaction.user = user
    interaction.guild = SimpleNamespace(id=guild_id) if in_guild else None
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup.send = AsyncMock()
    return interaction
