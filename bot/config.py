from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

DEFAULT_SERVICE_ACCOUNT_FILE = "credentials/service-account.json"
DEFAULT_WORKSHEET_NAME = "Members"
MIN_SCHEDULED_INTERVAL_MINUTES = 5

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class ConfigError(Exception):

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("Invalid configuration:\n- " + "\n- ".join(self.problems))


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    guild_id: int
    service_account_file: Path
    sheet_id: str
    worksheet_name: str
    hr_role_ids: tuple[int, ...]
    managed_roles: tuple[str, ...]
    sync_log_channel_id: int | None = None
    enable_scheduled_sync: bool = False
    scheduled_sync_interval_minutes: int = 60
    enable_join_sync: bool = False
    operation_delay_seconds: float = 0.2
    log_dir: Path = Path("logs")
    log_level: str = "INFO"

    def ensure_credentials_file(self) -> None:
       
        if not self.service_account_file.is_file():
            raise ConfigError(
                [
                    "GOOGLE_SERVICE_ACCOUNT_FILE does not point to an existing file "
                    f"(looked for: {self.service_account_file})."
                ]
            )

    def warnings(self) -> list[str]:
        
        notes: list[str] = []
        if not self.managed_roles:
            notes.append(
                "MANAGED_ROLES is empty: the bot will not modify any roles until it is set."
            )
        if not self.hr_role_ids:
            notes.append(
                "HR_ROLE_IDS is empty: only server administrators can run HR commands."
            )
        return notes

    def sanitized_summary(self) -> str:
        return (
            f"guild_id={self.guild_id} "
            f"worksheet={self.worksheet_name!r} "
            f"sheet_id={'set' if self.sheet_id else 'missing'} "
            f"token={'set' if self.discord_bot_token else 'missing'} "
            f"credentials_file={self.service_account_file} "
            f"hr_role_ids={len(self.hr_role_ids)} "
            f"managed_roles={list(self.managed_roles)} "
            f"log_channel={'set' if self.sync_log_channel_id else 'none'} "
            f"scheduled_sync={self.enable_scheduled_sync} "
            f"({self.scheduled_sync_interval_minutes} min) "
            f"join_sync={self.enable_join_sync}"
        )


def parse_bool(name: str, raw: str | None, default: bool, problems: list[str]) -> bool:
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    problems.append(f"{name} must be true or false (got an unrecognised value).")
    return default


def parse_int(name: str, raw: str | None, problems: list[str], *, required: bool) -> int | None:
    if raw is None or raw.strip() == "":
        if required:
            problems.append(f"{name} is required but not set.")
        return None
    value = raw.strip()
    if not value.isdigit() or int(value) <= 0:
        problems.append(f"{name} must be a positive whole number.")
        return None
    return int(value)


def parse_delay(name: str, raw: str | None, default: float, problems: list[str]) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        problems.append(f"{name} must be a number of seconds (e.g. 0.2).")
        return default
    if not 0 <= value <= 5:
        problems.append(f"{name} must be between 0 and 5 seconds.")
        return default
    return value


def parse_id_list(name: str, raw: str | None, problems: list[str]) -> tuple[int, ...]:
    if raw is None or raw.strip() == "":
        return ()
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            problems.append(f"{name} must be a comma-separated list of numeric role IDs.")
            return ()
        if int(part) not in ids:
            ids.append(int(part))
    return tuple(ids)


def parse_role_names(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    seen: set[str] = set()
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return tuple(names)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    problems: list[str] = []

    token = (source.get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        problems.append("DISCORD_BOT_TOKEN is required but not set.")

    guild_id = parse_int("DISCORD_GUILD_ID", source.get("DISCORD_GUILD_ID"), problems, required=True)

    sheet_id = (source.get("GOOGLE_SHEET_ID") or "").strip()
    if not sheet_id:
        problems.append("GOOGLE_SHEET_ID is required but not set.")

    sa_file = (source.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip() or DEFAULT_SERVICE_ACCOUNT_FILE
    worksheet = (source.get("GOOGLE_WORKSHEET_NAME") or "").strip() or DEFAULT_WORKSHEET_NAME

    hr_role_ids = parse_id_list("HR_ROLE_IDS", source.get("HR_ROLE_IDS"), problems)
    managed_roles = parse_role_names(source.get("MANAGED_ROLES"))

    log_channel = parse_int(
        "SYNC_LOG_CHANNEL_ID", source.get("SYNC_LOG_CHANNEL_ID"), problems, required=False
    )
    enable_scheduled = parse_bool(
        "ENABLE_SCHEDULED_SYNC", source.get("ENABLE_SCHEDULED_SYNC"), False, problems
    )
    interval = parse_int(
        "SCHEDULED_SYNC_INTERVAL_MINUTES",
        source.get("SCHEDULED_SYNC_INTERVAL_MINUTES"),
        problems,
        required=False,
    )
    if interval is not None and interval < MIN_SCHEDULED_INTERVAL_MINUTES:
        problems.append(
            f"SCHEDULED_SYNC_INTERVAL_MINUTES must be at least {MIN_SCHEDULED_INTERVAL_MINUTES}."
        )
    enable_join = parse_bool("ENABLE_JOIN_SYNC", source.get("ENABLE_JOIN_SYNC"), False, problems)

    delay = parse_delay(
        "OPERATION_DELAY_SECONDS", source.get("OPERATION_DELAY_SECONDS"), 0.2, problems
    )

    log_level = (source.get("LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        problems.append("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR.")
        log_level = "INFO"

    if problems:
        raise ConfigError(problems)

    assert guild_id is not None
    return Settings(
        
        discord_bot_token=token,guild_id=guild_id,service_account_file=Path(sa_file),sheet_id=sheet_id,
        worksheet_name=worksheet,hr_role_ids=hr_role_ids,managed_roles=managed_roles,sync_log_channel_id=log_channel,
        enable_scheduled_sync=enable_scheduled,scheduled_sync_interval_minutes=interval or 60,enable_join_sync=enable_join,
        operation_delay_seconds=delay,log_dir=Path((source.get("LOG_DIR") or "logs").strip() or "logs"),log_level=log_level,
    
    )
