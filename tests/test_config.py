import pytest

from bot.config import ConfigError, load_settings, parse_role_names

BASE = {
    "DISCORD_BOT_TOKEN": "tok-123456",
    "DISCORD_GUILD_ID": "123",
    "GOOGLE_SHEET_ID": "sheet",
}


def test_minimal_valid_config_uses_safe_defaults():
    s = load_settings(BASE)
    assert s.guild_id == 123 and s.worksheet_name == "Members"
    assert s.hr_role_ids == () and s.managed_roles == ()
    assert not s.enable_scheduled_sync and not s.enable_join_sync
    assert any("MANAGED_ROLES" in w for w in s.warnings())


def test_full_config_parsing():
    s = load_settings(
        {
            **BASE,
            "HR_ROLE_IDS": "1, 2,2,",
            "MANAGED_ROLES": " Developer, Designer ,developer,, HR ",
            "ENABLE_SCHEDULED_SYNC": "TRUE",
            "SCHEDULED_SYNC_INTERVAL_MINUTES": "15",
            "SYNC_LOG_CHANNEL_ID": "99",
        }
    )
    assert s.hr_role_ids == (1, 2)
    assert s.managed_roles == ("Developer", "Designer", "HR")
    assert s.enable_scheduled_sync and s.scheduled_sync_interval_minutes == 15
    assert s.sync_log_channel_id == 99


def test_all_problems_reported_together_without_secrets():
    with pytest.raises(ConfigError) as exc:
        load_settings({"DISCORD_GUILD_ID": "abc", "HR_ROLE_IDS": "1,x", "ENABLE_SCHEDULED_SYNC": "maybe"})
    text = str(exc.value)
    for name in ["DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "GOOGLE_SHEET_ID", "HR_ROLE_IDS", "ENABLE_SCHEDULED_SYNC"]:
        assert name in text


def test_interval_minimum_enforced():
    with pytest.raises(ConfigError):
        load_settings({**BASE, "SCHEDULED_SYNC_INTERVAL_MINUTES": "1"})


def test_sanitized_summary_hides_secrets():
    s = load_settings({**BASE, "GOOGLE_SHEET_ID": "SECRET-SHEET-ID"})
    assert "tok-123456" not in s.sanitized_summary()
    assert "SECRET-SHEET-ID" not in s.sanitized_summary()


def test_missing_credentials_file_detected(tmp_path):
    s = load_settings({**BASE, "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "nope.json")})
    with pytest.raises(ConfigError):
        s.ensure_credentials_file()
    (tmp_path / "ok.json").write_text("{}")
    load_settings({**BASE, "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "ok.json")}).ensure_credentials_file()


def test_parse_role_names_dedupes_case_insensitively():
    assert parse_role_names("A, a, B,,") == ("A", "B")
    assert parse_role_names("") == () and parse_role_names(None) == ()


def test_operation_delay_default_and_validation():
    assert load_settings(BASE).operation_delay_seconds == 0.2
    assert load_settings({**BASE, "OPERATION_DELAY_SECONDS": "0"}).operation_delay_seconds == 0
    for bad in ["abc", "-1", "99"]:
        with pytest.raises(ConfigError):
            load_settings({**BASE, "OPERATION_DELAY_SECONDS": bad})
