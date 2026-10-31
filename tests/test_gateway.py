from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.discord_gateway import DiscordGuildGateway, snapshot_from_member
from bot.sync import RoleOperationError
from tests.fakes import run


class FakeRole:
    def __init__(self, name, position, *, managed=False, default=False, rid=None):
        self.name, self.position, self.managed, self._default = name, position, managed, default
        self.id = rid or position + 100

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position


def http_error(cls, status):
    response = MagicMock(status=status, reason="x")
    return cls(response, "msg")


def make_guild(*, manage_roles=True, bot_top=10):
    roles = [
        FakeRole("@everyone", 0, default=True),
        FakeRole("Developer", 5),
        FakeRole("HR", 10),
        FakeRole("Admin", 20),
        FakeRole("Integration", 3, managed=True),
    ]
    me = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_roles=manage_roles), top_role=FakeRole("Bot", bot_top)
    )
    guild = MagicMock()
    guild.id = 1
    guild.me = me
    guild.roles = roles
    return guild


def test_list_roles_flags_hierarchy_managed_and_excludes_everyone():
    infos = {r.name: r for r in DiscordGuildGateway(make_guild()).listRoles()}
    assert "@everyone" not in infos
    assert infos["Developer"].problem is None
    assert "highest role" in infos["HR"].problem
    assert "highest role" in infos["Admin"].problem
    assert "integration" in infos["Integration"].problem


def test_list_roles_without_manage_roles_permission():
    infos = DiscordGuildGateway(make_guild(manage_roles=False)).listRoles()
    assert all(r.problem for r in infos)
    assert not DiscordGuildGateway(make_guild(manage_roles=False)).bot_can_manage_roles


def test_snapshot_uses_username_not_display_name_and_excludes_everyone():
    m = SimpleNamespace(
        id=7, name="the_username", display_name="Nick", nick="Nick", global_name="Global",
        roles=[FakeRole("@everyone", 0, default=True), FakeRole("Developer", 5)],
    )
    snap = snapshot_from_member(m)
    assert snap.username == "the_username" and snap.role_names == {"Developer"}


def test_fetch_members_pages_through_api_not_cache():
    guild = make_guild()

    async def fake_fetch(limit=None):
        assert limit is None
        for i in range(450):
            yield SimpleNamespace(id=i + 1, name=f"u{i}", roles=[FakeRole("@everyone", 0, default=True)])

    guild.fetch_members = fake_fetch
    members = run(DiscordGuildGateway(guild).getMembers())
    assert len(members) == 450


def test_add_role_calls_discord_with_reason():
    guild = make_guild()
    target = MagicMock()
    target.add_roles = AsyncMock()
    guild.get_member.return_value = target
    run(DiscordGuildGateway(guild).addRole(5, "Developer", "because"))
    role_arg = target.add_roles.await_args.args[0]
    assert role_arg.name == "Developer" and target.add_roles.await_args.kwargs["reason"] == "because"


def test_remove_role_calls_discord():
    guild = make_guild()
    target = MagicMock()
    target.remove_roles = AsyncMock()
    guild.get_member.return_value = target
    run(DiscordGuildGateway(guild).removeRole(5, "Developer", "because"))
    target.remove_roles.assert_awaited_once()


@pytest.mark.parametrize(
    "error,expected",
    [
        (http_error(discord.Forbidden, 403), "hierarchy"),
        (http_error(discord.NotFound, 404), "no longer exists"),
        (http_error(discord.HTTPException, 500), "HTTP 500"),
    ],
)
def test_discord_errors_become_role_operation_errors(error, expected):
    guild = make_guild()
    target = MagicMock()
    target.add_roles = AsyncMock(side_effect=error)
    guild.get_member.return_value = target
    with pytest.raises(RoleOperationError, match=expected):
        run(DiscordGuildGateway(guild).addRole(5, "Developer", "r"))


def test_member_missing_from_cache_is_fetched_and_gone_member_reported():
    guild = make_guild()
    guild.get_member.return_value = None
    guild.fetch_member = AsyncMock(side_effect=http_error(discord.NotFound, 404))
    with pytest.raises(RoleOperationError, match="no longer in the server"):
        run(DiscordGuildGateway(guild).addRole(5, "Developer", "r"))


def test_vanished_role_is_reported_not_created():
    guild = make_guild()
    with pytest.raises(RoleOperationError, match="no longer exists"):
        run(DiscordGuildGateway(guild).addRole(5, "Nonexistent", "r"))
