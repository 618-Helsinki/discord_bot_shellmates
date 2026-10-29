from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import gspread
from google.auth.exceptions import GoogleAuthError

logger = logging.getLogger("bot.sheets")

USERNAME_HEADER = "DISCORD USERNAME"
ROLE_HEADER = "ROLE"
ID_HEADER = "DISCORD ID"
READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

_DISCORD_ID_RE = re.compile(r"\d{17,20}")
_MAX_SNOWFLAKE = 2**64 - 1


class SheetsError(Exception):
    pass


class SheetValidationError(SheetsError):
    pass


def normalize_header(value: Any) -> str:
    return " ".join(str(value or "").split()).upper()


def normalize_username(value: str | None) -> str:
    text = (value or "").strip()
    if text.startswith("@"):
        text = text[1:]
    return text.lower()


def parse_roles(cell: str | None) -> tuple[str, ...]:
    seen: set[str] = set()
    roles: list[str] = []
    for part in (cell or "").split(","):
        name = part.strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            roles.append(name)
    return tuple(roles)


def parse_discord_id(cell: str | None) -> int | None:
    text = (cell or "").strip()
    if not text:
        return None
    if not _DISCORD_ID_RE.fullmatch(text) or int(text) > _MAX_SNOWFLAKE:
        raise ValueError("not a valid Discord ID (expected 17-20 digits, stored as plain text)")
    return int(text)


@dataclass(frozen=True)
class MemberRecord:
    row_number: int
    raw_username: str
    username: str
    discord_id: int | None
    roles: tuple[str, ...]

    @property
    def identifier(self) -> str:
        if self.raw_username.strip():
            return self.raw_username.strip()
        return str(self.discord_id) if self.discord_id is not None else f"row {self.row_number}"


@dataclass(frozen=True)
class RecordIssue:
    row_number: int
    identifier: str
    reason: str


@dataclass
class SheetData:
    records: list[MemberRecord] = field(default_factory=list)
    issues: list[RecordIssue] = field(default_factory=list)
    empty_rows: int = 0


def _cell(row: Sequence[Any], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    value = row[index]
    return "" if value is None else str(value)


def _find_columns(header_row: Sequence[Any]) -> tuple[int, int, int | None]:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, header in enumerate(header_row):
        key = normalize_header(header)
        if key:
            positions[key].append(index)

    missing = [h for h in (USERNAME_HEADER, ROLE_HEADER) if h not in positions]
    if missing:
        raise SheetValidationError(
            "Missing required column(s): " + ", ".join(missing) + ". "
            f"Row 1 must contain the headers '{USERNAME_HEADER}' and '{ROLE_HEADER}'."
        )
    duplicated = [h for h in (USERNAME_HEADER, ROLE_HEADER, ID_HEADER) if len(positions.get(h, [])) > 1]
    if duplicated:
        raise SheetValidationError(
            "Duplicate column header(s): " + ", ".join(duplicated) + ". Each header must appear once."
        )
    id_index = positions[ID_HEADER][0] if ID_HEADER in positions else None
    return positions[USERNAME_HEADER][0], positions[ROLE_HEADER][0], id_index


def parse_sheet(rows: Sequence[Sequence[Any]]) -> SheetData:
    if not rows:
        raise SheetValidationError("The worksheet is empty (no header row found).")

    user_col, role_col, id_col = _find_columns(rows[0])
    data = SheetData()
    candidates: list[MemberRecord] = []

    for row_number, row in enumerate(rows[1:], start=2):
        raw_user = _cell(row, user_col)
        raw_role = _cell(row, role_col)
        raw_id = _cell(row, id_col)

        if not (raw_user.strip() or raw_role.strip() or raw_id.strip()):
            data.empty_rows += 1
            continue

        discord_id: int | None = None
        if raw_id.strip():
            try:
                discord_id = parse_discord_id(raw_id)
            except ValueError as exc:
                data.issues.append(RecordIssue(row_number, raw_id.strip(), f"Invalid Discord ID: {exc}"))
                continue

        username = normalize_username(raw_user)
        identifier = raw_user.strip() or (str(discord_id) if discord_id else f"row {row_number}")
        if not username and discord_id is None:
            data.issues.append(
                RecordIssue(row_number, identifier, "Row has no Discord username and no Discord ID")
            )
            continue

        if raw_role.strip():
            roles = parse_roles(raw_role)
            if not roles:
                data.issues.append(
                    RecordIssue(row_number, identifier, "Role cell contains no valid role names")
                )
                continue
        else:
            roles = ()

        candidates.append(MemberRecord(row_number, raw_user, username, discord_id, roles))

    data.records, duplicate_issues = _drop_duplicates(candidates)
    data.issues.extend(duplicate_issues)
    data.issues.sort(key=lambda issue: issue.row_number)
    return data


def _drop_duplicates(records: list[MemberRecord]) -> tuple[list[MemberRecord], list[RecordIssue]]:
    by_id: dict[int, list[MemberRecord]] = defaultdict(list)
    by_name: dict[str, list[MemberRecord]] = defaultdict(list)
    for record in records:
        if record.discord_id is not None:
            by_id[record.discord_id].append(record)
        else:
            by_name[record.username].append(record)

    rejected: dict[int, RecordIssue] = {}
    for group_key, group, label in (
        *((k, g, "Discord ID") for k, g in by_id.items()),
        *((k, g, "username") for k, g in by_name.items()),
    ):
        if len(group) < 2:
            continue
        rows = ", ".join(str(r.row_number) for r in group)
        for record in group:
            rejected[record.row_number] = RecordIssue(
                record.row_number,
                record.identifier,
                f"Duplicate {label} appears on rows {rows}; all of these rows were skipped as ambiguous",
            )

    accepted = [r for r in records if r.row_number not in rejected]
    return accepted, list(rejected.values())


class SheetsClient:
    def __init__(
        self,
        service_account_file: Path | str,
        sheet_id: str,
        worksheet_name: str,
        *,
        authorize: Callable[..., Any] | None = None,
    ) -> None:
        self._service_account_file = Path(service_account_file)
        self._sheet_id = sheet_id
        self._worksheet_name = worksheet_name
        self._authorize = authorize or gspread.service_account

    def read_rows(self) -> list[list[str]]:
        if not self._worksheet_name:
            raise SheetsError("No worksheet name is configured (GOOGLE_WORKSHEET_NAME).")
        try:
            client = self._authorize(
                filename=str(self._service_account_file), scopes=[READONLY_SCOPE]
            )
            worksheet = client.open_by_key(self._sheet_id).worksheet(self._worksheet_name)
            return worksheet.get_all_values()
        except gspread.exceptions.SpreadsheetNotFound as exc:
            raise SheetsError(
                "Spreadsheet not found. Check GOOGLE_SHEET_ID and that the sheet is shared "
                "with the service account's email address."
            ) from exc
        except gspread.exceptions.WorksheetNotFound as exc:
            raise SheetsError(
                f"Worksheet '{self._worksheet_name}' was not found in the spreadsheet."
            ) from exc
        except gspread.exceptions.APIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("Google Sheets API error (HTTP %s)", status)
            if status in (401, 403):
                raise SheetsError(
                    "Google denied access (HTTP %s). Share the sheet with the service account "
                    "and make sure the Google Sheets API is enabled." % status
                ) from exc
            if status == 404:
                raise SheetsError("Spreadsheet not found (HTTP 404). Check GOOGLE_SHEET_ID.") from exc
            if status == 429:
                raise SheetsError("Google Sheets rate limit reached. Try again in a minute.") from exc
            raise SheetsError(f"Google Sheets API error (HTTP {status}).") from exc
        except FileNotFoundError as exc:
            raise SheetsError(
                "Google service-account credentials file was not found "
                "(GOOGLE_SERVICE_ACCOUNT_FILE)."
            ) from exc
        except (GoogleAuthError, ValueError) as exc:
            logger.warning("Google credential problem: %s", type(exc).__name__)
            raise SheetsError(
                "Google credentials are invalid or unreadable. Re-download the service-account JSON."
            ) from exc
        except OSError as exc:
            logger.warning("Network problem contacting Google: %s", type(exc).__name__)
            raise SheetsError("Could not reach Google Sheets (network error).") from exc
        except Exception as exc:
            logger.exception("Unexpected error reading Google Sheet")
            raise SheetsError("Unexpected error while reading Google Sheets.") from exc

    async def fetch_sheet_data(self) -> SheetData:
        rows = await asyncio.to_thread(self.read_rows)
        return parse_sheet(rows)
