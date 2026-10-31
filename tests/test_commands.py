import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot.commands import BUSY_MESSAGE, CommandHandlers, register_commands
from bot.config import load_settings
from bot.permissions import UNAUTHORIZED_MESSAGE
from bot.reporting import (
    DISCORD_MESSAGE_LIMIT,
    format_detailed_report,
    format_summary,
    truncate,
)
from bot.sheets import SheetsError, parse_sheet
from bot.sync import SyncReport, SyncService
from tests.fakes import (
    FakeGateway,
    FakeSheetSource,
    SlowSheetSource,
    fake_interaction,
    member,
    role_infos,
    run,
)

ROLES = ["Admin", "Developer", "Designer", "HR"]
HR_ROLE = 777
SETTINGS = load_settings(
    {
        "DISCORD_BOT_TOKEN": "tok-123456",
        "DISCORD_GUILD_ID": "1",
        "GOOGLE_SHEET_ID": "s",
        "HR_ROLE_IDS": str(HR_ROLE),
        "MANAGED_ROLES": "Developer,Designer,HR",
    }
)


def sheet(*rows):
    return parse_sheet([["DISCORD USERNAME", "ROLE"], *[list(r) for r in rows]])


def make(data=None, error=None, members=None):
    members = members or [member(1, "m1", "HR")]
    gateway = FakeGateway(members, role_infos(*ROLES))
    service = SyncService(FakeSheetSource(data, error), SETTINGS.managed_roles)
    handlers = CommandHandlers(SETTINGS, service, gateway_factory=lambda guild: gateway)
    return handlers, gateway, service


def sent(interaction):
    out = []
    for call in interaction.followup.send.await_args_list:
        data = dict(call.kwargs)
        if call.args:
            data["content"] = call.args[0]
        out.append(data)
    return out


def test_unauthorized_user_is_refused_politely_and_nothing_runs():
    handlers, gateway, service = make(sheet(("m1", "Developer")))
    interaction = fake_interaction(role_ids=[5])
    run(handlers.sync_roles(interaction, dry_run=False))
    interaction.response.send_message.assert_awaited_once_with(UNAUTHORIZED_MESSAGE, ephemeral=True)
    interaction.response.defer.assert_not_awaited()
    assert gateway.calls == [] and service._sheet_source.calls == 0


@pytest.mark.parametrize("command", ["sync", "preview", "check"])
def test_every_command_checks_authorization(command):
    handlers, gateway, _ = make(sheet(("m1", "Developer")))
    interaction = fake_interaction(role_ids=[5])
    if command == "sync":
        run(handlers.sync_roles(interaction, False))
    elif command == "preview":
        run(handlers.preview_sync(interaction))
    else:
        run(handlers.check_member(interaction, SimpleNamespace(id=1, name="m1", roles=[])))
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.args[0] == UNAUTHORIZED_MESSAGE
    assert gateway.calls == []


def test_command_in_dm_is_refused():
    handlers, gateway, _ = make(sheet(("m1", "Developer")))
    interaction = fake_interaction(in_guild=False, admin=True)
    run(handlers.sync_roles(interaction, False))
    assert gateway.calls == []
    interaction.response.defer.assert_not_awaited()


def test_hr_member_live_sync_defers_applies_and_replies_ephemerally():
    handlers, gateway, _ = make(sheet(("m1", "Developer")))
    interaction = fake_interaction(role_ids=[HR_ROLE])
    run(handlers.sync_roles(interaction, dry_run=False))
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert sorted(gateway.calls) == [("add", 1, "Developer"), ("remove", 1, "HR")]
    final = sent(interaction)[-1]
    assert final["ephemeral"] is True
    assert "Roles added: 1" in final["content"]
    assert isinstance(final["file"], discord.File)


def test_admin_without_hr_role_may_sync():
    handlers, gateway, _ = make(sheet(("m1", "Developer")))
    run(handlers.sync_roles(fake_interaction(admin=True), False))
    assert gateway.calls


def test_dry_run_never_calls_role_methods():
    handlers, gateway, _ = make(sheet(("m1", "Developer")))
    interaction = fake_interaction(admin=True)
    run(handlers.sync_roles(interaction, dry_run=True))
    assert gateway.calls == []
    assert "DRY RUN" in sent(interaction)[-1]["content"]


def test_preview_sync_is_a_dry_run_with_same_logic():
    handlers, gateway, _ = make(sheet(("m1", "Developer")))
    interaction = fake_interaction(admin=True)
    run(handlers.preview_sync(interaction))
    assert gateway.calls == []
    content = sent(interaction)[-1]["content"]
    assert "DRY RUN" in content and "Roles that will be added: 1" in content


def test_sheet_failure_reports_abort_and_changes_nothing():
    handlers, gateway, _ = make(error=SheetsError("Google denied access"))
    interaction = fake_interaction(admin=True)
    run(handlers.sync_roles(interaction, False))
    assert gateway.calls == []
    text = sent(interaction)[-1]["content"]
    assert "no roles changed" in text and "Google denied access" in text


def test_unexpected_crash_gives_generic_message_without_details():
    handlers, gateway, service = make(sheet(("m1", "Developer")))
    service.run = AsyncMock(side_effect=RuntimeError("secret-internal-detail"))
    interaction = fake_interaction(admin=True)
    run(handlers.sync_roles(interaction, False))
    assert "secret-internal-detail" not in sent(interaction)[-1]["content"]


def test_busy_message_when_sync_already_running():
    async def scenario():
        source = SlowSheetSource(sheet(("m1", "Developer")))
        gateway = FakeGateway([member(1, "m1")], role_infos(*ROLES))
        service = SyncService(source, SETTINGS.managed_roles)
        handlers = CommandHandlers(SETTINGS, service, gateway_factory=lambda g: gateway)
        first = asyncio.create_task(handlers.sync_roles(fake_interaction(admin=True), True))
        await asyncio.sleep(0.01)
        second = fake_interaction(admin=True)
        await handlers.sync_roles(second, False)
        second.response.send_message.assert_awaited_once_with(BUSY_MESSAGE, ephemeral=True)
        assert gateway.calls == []
        source.release.set()
        await first

    run(scenario())


def test_notifier_receives_counts_only_report():
    handlers, gateway, service = make(sheet(("m1", "Developer")))
    notifier = AsyncMock()
    handlers._notifier = notifier
    run(handlers.sync_roles(fake_interaction(admin=True), False))
    notifier.assert_awaited_once()


def test_notifier_failure_does_not_break_command():
    handlers, _, _ = make(sheet(("m1", "Developer")))
    handlers._notifier = AsyncMock(side_effect=RuntimeError("channel gone"))
    interaction = fake_interaction(admin=True)
    run(handlers.sync_roles(interaction, False))
    assert "Roles added" in sent(interaction)[-1]["content"]


def test_check_member_is_read_only_and_reports_differences():
    handlers, gateway, _ = make(sheet(("m1", "Designer")), members=[member(1, "m1", "Developer")])
    discord_member = SimpleNamespace(
        id=1, name="m1", roles=[SimpleNamespace(name="Developer", is_default=lambda: False)]
    )
    interaction = fake_interaction(admin=True)
    run(handlers.check_member(interaction, discord_member))
    text = sent(interaction)[-1]["content"]
    assert "Would add: Designer" in text and "Would remove: Developer" in text
    assert gateway.calls == []


def test_summary_distinguishes_dry_run_from_live():
    live = SyncReport(dry_run=False, initiated_by="x", roles_added=3, roles_removed=1)
    dry = SyncReport(dry_run=True, initiated_by="x", planned_additions=3, planned_removals=1)
    assert "DRY RUN" not in format_summary(live) and "Roles added: 3" in format_summary(live)
    assert "DRY RUN (no changes)" in format_summary(dry)


def test_summary_stays_within_discord_limit_with_many_errors():
    report = SyncReport(dry_run=False, initiated_by="x")
    report.errors = [f"Could not add role 'R' for user{i}: " + "x" * 200 for i in range(500)]
    report.warnings = ["w" * 300] * 50
    assert len(format_summary(report)) <= DISCORD_MESSAGE_LIMIT


def test_detailed_report_contains_failures_and_partial_note():
    handlers, gateway, service = make(sheet(("m1", "Developer")))
    gateway._fail[(1, "Developer", "add")] = "Discord refused"
    report = run(service.run(gateway, dry_run=False, initiated_by="t"))
    text = format_detailed_report(report)
    assert "FAILED" in text and "Discord refused" in text and "partially" in text
    assert report.roles_added == 0


def test_truncate():
    assert truncate("short") == "short"
    assert len(truncate("x" * 5000)) <= 1900


def test_commands_register_with_discord_py():
    from discord import app_commands

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    handlers, _, _ = make(sheet())
    register_commands(tree, handlers, discord.Object(id=1))
    names = {c.name for c in tree.get_commands(guild=discord.Object(id=1))}
    assert names == {"sync-roles", "preview-sync", "check-member"}
    sync = tree.get_command("sync-roles", guild=discord.Object(id=1))
    assert [p.name for p in sync.parameters] == ["dry_run"]
    assert sync.parameters[0].required is False


def test_summary_and_detailed_report_show_duration():
    report = SyncReport(dry_run=False, initiated_by="x", duration_seconds=12.34)
    assert "Duration: 12.3s" in format_summary(report)
    assert "12.3 s" in format_detailed_report(report)


def test_reports_show_skipped_roles():
    handlers, gateway, service = make(sheet(("m1", "Developer, Desginer")))
    report = run(service.run(gateway, dry_run=False, initiated_by="t"))
    assert "Invalid roles skipped: 1" in format_summary(report)
    detail = format_detailed_report(report)
    assert "SKIPPED ROLES" in detail and "Desginer" in detail and "does not exist in Discord" in detail
    assert report.roles_added == 1


def test_summary_labels_members_processed_not_modified():
    report = SyncReport(dry_run=True, initiated_by="x", members_processed=3, members_unchanged=1)
    text = format_summary(report)
    assert "Members processed: 3" in text
    assert "Members modified" not in text
    assert "Members unchanged: 1" in text


def test_detailed_report_lines_are_aligned():
    report = SyncReport(dry_run=False, initiated_by="x", roles_added=2, roles_removed=1)
    lines = format_detailed_report(report).splitlines()
    assert any(line.startswith("Roles removed") for line in lines)
    assert not any(line.startswith("  Roles removed") for line in lines)
