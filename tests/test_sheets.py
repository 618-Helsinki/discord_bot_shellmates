from unittest.mock import MagicMock

import gspread
import pytest
from google.auth.exceptions import MalformedError

from bot.sheets import (
    SheetsClient,
    SheetsError,
    SheetValidationError,
    normalize_header,
    normalize_username,
    parse_discord_id,
    parse_roles,
    parse_sheet,
)
from tests.fakes import run

HEADER = ["DISCORD USERNAME", "ROLE"]
GOOD_ID = "123456789012345678"
GOOD_ID_2 = "234567890123456789"


@pytest.mark.parametrize(
    "raw", ["member1", "Member1", " MEMBER1", "@member1", " @Member1", "   @Member1   "]
)
def test_username_variants_normalise_identically(raw):
    assert normalize_username(raw) == "member1"


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_usernames_normalise_to_empty(raw):
    assert normalize_username(raw) == ""


def test_only_one_leading_at_is_removed():
    assert normalize_username("@@member1") == "@member1"


def test_inner_characters_and_whitespace_are_preserved():
    assert normalize_username("@Mem ber.1_x") == "mem ber.1_x"
    assert normalize_username("a@b") == "a@b"


def test_whitespace_after_at_is_not_guessed():
    assert normalize_username("@ member1") == " member1"


def test_header_normalisation():
    assert normalize_header("  discord   username ") == "DISCORD USERNAME"


def test_parse_roles_splits_trims_and_dedupes():
    assert parse_roles("Developer, Designer") == ("Developer", "Designer")
    assert parse_roles(" Developer ,, Designer ,") == ("Developer", "Designer")
    assert parse_roles("Developer, developer, Developer") == ("Developer",)
    assert parse_roles("Content Writer") == ("Content Writer",)
    assert parse_roles("") == () and parse_roles(None) == ()


def test_parse_discord_id():
    assert parse_discord_id(GOOD_ID) == int(GOOD_ID)
    assert parse_discord_id("  ") is None
    for bad in ["abc", "123", "1.23457E+17", "-123456789012345678", "123456789012345678901"]:
        with pytest.raises(ValueError):
            parse_discord_id(bad)


def test_valid_sheet():
    data = parse_sheet([HEADER, ["member1", "Developer"], ["member2", "Designer, HR"]])
    assert [(r.username, r.roles) for r in data.records] == [
        ("member1", ("Developer",)),
        ("member2", ("Designer", "HR")),
    ]
    assert data.issues == []


def test_headers_case_and_whitespace_insensitive_and_reorderable():
    rows = [["  role ", "Discord   Username", "notes"], ["Developer", "member1", "x"]]
    data = parse_sheet(rows)
    assert data.records[0].username == "member1" and data.records[0].roles == ("Developer",)


@pytest.mark.parametrize(
    "header", [["ROLE"], ["DISCORD USERNAME"], ["Name", "Title"], []]
)
def test_missing_headers_raise(header):
    with pytest.raises(SheetValidationError):
        parse_sheet([header, ["a", "b"]])


def test_empty_worksheet_raises():
    with pytest.raises(SheetValidationError):
        parse_sheet([])


def test_duplicate_headers_raise():
    with pytest.raises(SheetValidationError):
        parse_sheet([["DISCORD USERNAME", "ROLE", "role"], ["a", "b", "c"]])


def test_empty_rows_are_harmless():
    data = parse_sheet([HEADER, ["", ""], ["   ", " "], [], ["member1", "HR"]])
    assert data.empty_rows == 3 and len(data.records) == 1 and data.issues == []


def test_short_rows_do_not_crash():
    data = parse_sheet([HEADER, ["member1"]])
    assert data.records[0].roles == ()


def test_row_with_role_but_no_identifier_is_an_issue():
    data = parse_sheet([HEADER, ["", "Developer"]])
    assert data.records == [] and "no Discord username" in data.issues[0].reason


def test_empty_role_cell_is_explicit_empty_set():
    data = parse_sheet([HEADER, ["member1", ""]])
    assert data.records[0].roles == () and data.issues == []


def test_role_cell_of_only_commas_is_rejected_not_treated_as_empty():
    data = parse_sheet([HEADER, ["member1", " , ,"]])
    assert data.records == [] and len(data.issues) == 1


def test_duplicate_usernames_skip_all_involved_rows():
    data = parse_sheet([HEADER, ["Member1", "HR"], ["@member1", "Developer"], ["other", "HR"]])
    assert [r.username for r in data.records] == ["other"]
    assert {i.row_number for i in data.issues} == {2, 3}
    assert all("Duplicate username" in i.reason for i in data.issues)


def test_duplicate_discord_ids_skip_all_involved_rows():
    rows = [HEADER + ["DISCORD ID"], ["a", "HR", GOOD_ID], ["b", "HR", GOOD_ID], ["c", "HR", GOOD_ID_2]]
    data = parse_sheet(rows)
    assert [r.username for r in data.records] == ["c"]
    assert {i.row_number for i in data.issues} == {2, 3}


def test_invalid_id_rejected_without_username_fallback():
    data = parse_sheet([HEADER + ["DISCORD ID"], ["member1", "HR", "12345"]])
    assert data.records == [] and "Invalid Discord ID" in data.issues[0].reason


def test_id_only_row_is_valid():
    data = parse_sheet([HEADER + ["DISCORD ID"], ["", "HR", GOOD_ID]])
    assert data.records[0].discord_id == int(GOOD_ID)


def test_non_string_cells_are_tolerated():
    data = parse_sheet([HEADER, [None, None], ["member1", 5]])
    assert data.records[0].roles == ("5",)


def test_malformed_rows_never_become_destructive_instructions():
    rows = [HEADER + ["DISCORD ID"], ["", "", ""], ["", "HR", ""], ["m1", ",", ""], ["m2", "HR", "xx"]]
    assert parse_sheet(rows).records == []


def make_client(worksheet_name="Members", authorize=None):
    return SheetsClient("creds.json", "sheet-id", worksheet_name, authorize=authorize)


def api_error(status):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"error": {"code": status, "message": "x", "status": "S"}}
    return gspread.exceptions.APIError(response)


def test_client_reads_rows_with_readonly_scope():
    ws = MagicMock()
    ws.get_all_values.return_value = [HEADER, ["m", "HR"]]
    gc = MagicMock()
    gc.open_by_key.return_value.worksheet.return_value = ws
    authorize = MagicMock(return_value=gc)
    rows = make_client(authorize=authorize).read_rows()
    assert rows == [HEADER, ["m", "HR"]]
    assert authorize.call_args.kwargs["scopes"] == ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    gc.open_by_key.assert_called_with("sheet-id")


def test_client_fetch_sheet_data_parses_without_blocking():
    ws = MagicMock()
    ws.get_all_values.return_value = [HEADER, ["m", "HR"]]
    gc = MagicMock()
    gc.open_by_key.return_value.worksheet.return_value = ws
    data = run(make_client(authorize=MagicMock(return_value=gc)).fetch_sheet_data())
    assert data.records[0].username == "m"


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("nope"),
        MalformedError("bad json"),
        ValueError("bad"),
        ConnectionError("down"),
        gspread.exceptions.SpreadsheetNotFound(MagicMock()),
        api_error(403),
        api_error(404),
        api_error(429),
        api_error(500),
        RuntimeError("anything"),
    ],
)
def test_google_failures_become_sheets_error(exc):
    with pytest.raises(SheetsError):
        make_client(authorize=MagicMock(side_effect=exc)).read_rows()


def test_worksheet_not_found_message_names_worksheet():
    gc = MagicMock()
    gc.open_by_key.return_value.worksheet.side_effect = gspread.exceptions.WorksheetNotFound("x")
    with pytest.raises(SheetsError, match="Members"):
        make_client(authorize=MagicMock(return_value=gc)).read_rows()


def test_missing_worksheet_configuration():
    with pytest.raises(SheetsError, match="worksheet name"):
        make_client(worksheet_name="").read_rows()


def test_error_messages_do_not_leak_exception_details():
    secret = "PRIVATE-KEY-DATA-XYZ"
    with pytest.raises(SheetsError) as exc:
        make_client(authorize=MagicMock(side_effect=ValueError(secret))).read_rows()
    assert secret not in str(exc.value)
