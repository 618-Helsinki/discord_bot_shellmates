import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

from bot.config import load_settings
from bot.logging_config import audit_event, register_secret, setup_logging
from bot.main import HRBot
from bot.sheets import parse_sheet
from bot.sync import SyncService
from tests.fakes import FakeGateway, FakeSheetSource, member, role_infos, run

TOKEN = "SUPER-SECRET-TOKEN-VALUE"


def flush(logger_name):
    for h in logging.getLogger(logger_name).handlers:
        h.flush()


def test_audit_log_written_to_file_with_expected_fields(tmp_path):
    setup_logging(tmp_path, "INFO")
    data = parse_sheet([["DISCORD USERNAME", "ROLE"], ["m1", "Developer"], ["ghost", "HR"]])
    svc = SyncService(FakeSheetSource(data), ["Developer", "HR"])
    gw = FakeGateway([member(1, "m1")], role_infos("Developer", "HR"), guild_id=555)
    run(svc.run(gw, dry_run=False, initiated_by="alice (42)"))
    flush("bot.audit")
    text = (tmp_path / "sync.log").read_text()
    assert 'event=role_add' in text and 'role="Developer"' in text and 'outcome="applied"' in text
    assert 'initiator="alice (42)"' in text and 'guild="555"' in text
    assert "event=record_skipped" in text and "ghost" in text
    assert "event=sync_finished" in text


def test_audit_values_cannot_forge_log_lines(tmp_path):
    setup_logging(tmp_path, "INFO")
    audit_event(logging.getLogger("bot.audit"), "x", detail="a\nevent=forged")
    flush("bot.audit")
    lines = (tmp_path / "sync.log").read_text().splitlines()
    assert len(lines) == 1


def test_registered_secrets_are_redacted_from_logs(tmp_path, capsys):
    setup_logging(tmp_path, "INFO")
    register_secret(TOKEN)
    logging.getLogger("bot.test").error("token is %s", TOKEN)
    audit_event(logging.getLogger("bot.audit"), "leak", detail=TOKEN)
    flush("bot.audit")
    assert TOKEN not in capsys.readouterr().err
    assert TOKEN not in (tmp_path / "sync.log").read_text()


def test_start_without_config_fails_clearly_and_leaks_nothing(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "bot.main"],
        cwd=Path(__file__).resolve().parents[1],
        env={"PATH": "", "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "DISCORD_BOT_TOKEN is required" in result.stderr


SETTINGS = load_settings(
    {
        "DISCORD_BOT_TOKEN": TOKEN,
        "DISCORD_GUILD_ID": "1",
        "GOOGLE_SHEET_ID": "s",
        "MANAGED_ROLES": "Developer",
        "ENABLE_SCHEDULED_SYNC": "true",
        "SCHEDULED_SYNC_INTERVAL_MINUTES": "10",
    }
)


def test_bot_requests_members_intent_and_scheduled_job_starts_once():
    async def scenario():
        bot = HRBot(SETTINGS)
        assert bot.intents.members is True
        bot.tree.sync = AsyncMock(return_value=[])
        bot.wait_until_ready = AsyncMock()
        await bot.setup_hook()
        loop = bot._scheduled_loop
        assert loop is not None and loop.is_running()
        await bot.setup_hook()
        assert bot._scheduled_loop is loop
        await bot.close()

    run(scenario())


def test_scheduled_sync_skips_first_immediate_tick_and_runs_afterwards():
    async def scenario():
        bot = HRBot(SETTINGS)
        bot.service.run = AsyncMock()
        bot._scheduled_loop = type("L", (), {"current_loop": 0})()
        await bot._scheduled_sync()
        bot.service.run.assert_not_awaited()

    run(scenario())


def test_scheduled_sync_is_disabled_by_default():
    assert load_settings({"DISCORD_BOT_TOKEN": "x" * 8, "DISCORD_GUILD_ID": "1", "GOOGLE_SHEET_ID": "s"}).enable_scheduled_sync is False


def test_join_sync_ignored_when_disabled():
    async def scenario():
        s = load_settings({"DISCORD_BOT_TOKEN": "x" * 8, "DISCORD_GUILD_ID": "1", "GOOGLE_SHEET_ID": "s"})
        bot = HRBot(s)
        bot.service.sync_new_member = AsyncMock()
        await bot.on_member_join(type("M", (), {"guild": type("G", (), {"id": 1})(), "id": 9})())
        bot.service.sync_new_member.assert_not_awaited()

    run(scenario())
