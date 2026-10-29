# Discord HR Role Management Bot

A Python Discord bot that keeps server roles in sync with a **Google Sheet**.
HR edits a spreadsheet; the bot adds missing roles and removes outdated ones, and
never touches roles it has not been explicitly told to manage.

- [1. Overview](#1-overview)
- [2. Requirements](#2-requirements)
- [3. Installation](#3-installation)
- [4. Discord Developer Portal setup](#4-discord-developer-portal-setup)
- [5. Google Sheets setup](#5-google-sheets-setup)
- [6. Environment configuration](#6-environment-configuration)
- [7. Running the bot](#7-running-the-bot)
- [8. Using the commands](#8-using-the-commands)
- [9. Testing](#9-testing)
- [10. Troubleshooting](#10-troubleshooting)
- [11. Security notes and known limitations](#11-security-notes-and-known-limitations)
- [12. Optional: CI](#12-optional-ci)

---

## 1. Overview

**What it does.** `/sync-roles` reads a worksheet, matches each row to a server
member, compares the member's current *managed* roles with the roles in the
sheet, then adds what is missing and removes what is outdated.

**Why Google Sheets is the source of truth.** HR already works in spreadsheets, and
a sheet gives them history, sharing and review for free. For the roles the bot
manages, the sheet always wins; roles outside the managed allowlist are invisible
to the bot.

### Features

| Feature | Status |
|---|---|
| `/sync-roles` with validation, member/role resolution, summary report | **Required** |
| HR-role / administrator authorization | **Required** |
| Managed-role allowlist; unmanaged roles always preserved | **Required** |
| Fatal errors (sheet unreachable, bad headers, bad config) change nothing | **Required** |
| In-process protection against overlapping syncs | **Required** |
| `dry_run` option and `/preview-sync` | Optional (implemented) |
| Multiple roles per cell (`Developer, Designer`) | Optional (implemented) |
| `DISCORD ID` column (preferred over username) | Optional (implemented) |
| `.txt` detail report attached to the reply | Optional (implemented) |
| Audit log in `logs/sync.log` | Optional (implemented) |
| `/check-member` | Optional (implemented) |
| Scheduled sync (`ENABLE_SCHEDULED_SYNC`, off by default) | Optional (implemented) |
| Role grant when a member joins (`ENABLE_JOIN_SYNC`, off by default) | Optional (implemented) |
| GitHub Actions test workflow | Optional (implemented, see §12) |

---

## 2. Requirements

- **Python 3.11 or newer** (developed and tested on 3.12)
- A Discord account with permission to add bots to your server
- A Google account (for the Cloud project and the spreadsheet)

Check Python: `python --version` (on some systems `python3 --version`). Install it
from <https://www.python.org/downloads/> if needed.

---

## 3. Installation

```bash
git clone <your-repository-url>     # placeholder: use your own repository URL
cd discord-hr-bot

python -m venv .venv
```

Activate the virtual environment:

```powershell
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Create your private configuration file:

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

---

## 4. Discord Developer Portal setup

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Open the **Bot** page. (The bot user is created with the application.)
3. Click **Reset Token**, copy the token and put it in `.env` as `DISCORD_BOT_TOKEN`.
   **Treat it like a password.** If it ever leaks, reset it immediately.
4. On the same **Bot** page, under **Privileged Gateway Intents**, turn on
   **Server Members Intent** and save. *The bot cannot start without it.*
   (The code also requests it: `intents.members = True`.)
5. Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: **Manage Roles** (that is all that is required;
     do **not** grant Administrator). Add **View Channels** and **Send Messages**
     only if you use `SYNC_LOG_CHANNEL_ID`.
   - Open the generated URL and add the bot to your server.
6. **Role hierarchy (important).** In *Server Settings → Roles*, drag the bot's role
   **above every role it should manage** (e.g. above `Developer`, `Designer`,
   `HR`). Discord refuses role changes for roles at or above the bot's top role, no
   matter what the code does. The bot also cannot change roles of members whose
   top role is above its own (e.g. the server owner).
7. In Discord, enable **Settings → Advanced → Developer Mode**, then:
   - right-click your server icon → **Copy Server ID** → `DISCORD_GUILD_ID`
   - *Server Settings → Roles*, right-click an HR role → **Copy Role ID** →
     `HR_ROLE_IDS` (comma-separated for several)

---

## 5. Google Sheets setup

1. Open <https://console.cloud.google.com/> and **create a project**.
2. **APIs & Services → Library →** search **Google Sheets API → Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account**. Give it
   a name and finish (no extra roles needed).
4. Open the service account → **Keys → Add key → Create new key → JSON**. A file is
   downloaded. Save it as `credentials/service-account.json` inside the project
   (the `credentials/` folder is git-ignored). **Never commit it.**
5. Open the JSON file, copy the `client_email` value (looks like
   `name@project.iam.gserviceaccount.com`), then in your spreadsheet click **Share**
   and give that address **Viewer** access (the bot only reads).
6. Copy the **spreadsheet ID** from its URL
   `https://docs.google.com/spreadsheets/d/`**`<THIS_PART>`**`/edit` into
   `GOOGLE_SHEET_ID`.
7. Name the worksheet tab (default `Members`) and set `GOOGLE_WORKSHEET_NAME`.
8. Put the headers in **row 1**:

| DISCORD USERNAME | ROLE | DISCORD ID *(optional)* |
|---|---|---|
| member1 | Developer | |
| member2 | Designer | |
| member3 | Developer, Content | |
| member4 | HR | |
| member5 | | |

A ready-to-import example is in `sample_data/sample_members.csv`
(Google Sheets: *File → Import → Upload*).

**Header rules:** names are case-insensitive and extra spaces are ignored; column
order does not matter; `DISCORD USERNAME` and `ROLE` are required.

**Discord ID column tip:** format that column as **Plain text** *before* pasting
IDs. Sheets treats long numbers as numbers and silently rounds them (or shows
`1.23E+17`), which makes the ID invalid; the bot rejects such rows rather than
guessing.

**Test the connection** without changing anything: after finishing §6–§7, run
`/preview-sync` in Discord. A successful preview proves Google access works.

---

## 6. Environment configuration

`.env.example` is a **template and safe to commit**. `.env` holds real secrets and
must stay private (it is in `.gitignore`).

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | Bot token (§4). Never printed or logged. |
| `DISCORD_GUILD_ID` | yes | The one server this bot serves. Commands are refused elsewhere. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | yes | Path to the service-account JSON (default `credentials/service-account.json`). |
| `GOOGLE_SHEET_ID` | yes | Spreadsheet ID. |
| `GOOGLE_WORKSHEET_NAME` | no | Tab name (default `Members`). |
| `HR_ROLE_IDS` | no | Comma-separated **role IDs** allowed to run commands. Empty = administrators only. |
| `MANAGED_ROLES` | yes\* | Comma-separated role **names** the bot may add/remove. \*If empty the bot starts but refuses to sync. |
| `SYNC_LOG_CHANNEL_ID` | no | Channel for a counts-only summary after each sync. |
| `ENABLE_SCHEDULED_SYNC` | no | `true`/`false` (default `false`). |
| `SCHEDULED_SYNC_INTERVAL_MINUTES` | no | Default `60`, minimum `5`. First run is one interval after startup. |
| `ENABLE_JOIN_SYNC` | no | `true`/`false` (default `false`). |
| `OPERATION_DELAY_SECONDS` | no | Pause between role updates (default `0.2`, range 0-5). discord.py also handles Discord's rate limits automatically. |
| `LOG_DIR`, `LOG_LEVEL` | no | Default `logs` / `INFO`. |

**Managed-role allowlist.** Only roles named in `MANAGED_ROLES` can ever be added or
removed. Every one of them must exist in the server (the bot never creates roles);
if one is missing, the sync aborts before changing anything.

**Authorization.** A user may run commands if they are a server **administrator** or
hold a role whose **ID** is in `HR_ROLE_IDS`. Role names are never used for
authorization.

Invalid values (e.g. a non-numeric ID) stop startup with a message naming the
setting, never its value.

---

## 7. Running the bot

```bash
python -m bot.main
```

Successful startup looks like:

```
... INFO bot.main: Starting with configuration: guild_id=... token=set ...
... INFO bot.main: Synced 3 slash command(s) to guild ...
... INFO bot.main: Ready as HRBot#1234 in guild 'My Server' (...)
```

The bot shows as online and `/sync-roles`, `/preview-sync`, `/check-member` appear
in your server (slash commands can take a minute to show; restart your Discord
client if they do not).

---

## 8. Using the commands

All commands are restricted to HR roles and administrators, work only in the
configured server, and reply **ephemerally** (only you see the reply, since reports
contain member names).

### `/preview-sync`
Reads and validates the sheet and shows what **would** change. **Nothing is
modified.** It uses exactly the same code path as a real sync.

### `/sync-roles`
Performs a **real** synchronization. Use `/preview-sync` first when in doubt.

### `/sync-roles dry_run:true`
Identical to `/preview-sync`.

### `/check-member member:@someone`
Compares one member with the sheet and shows what would be added/removed. Read-only.

### What a sync does

For every valid row, the member's **managed** roles are made to equal the roles in
the sheet. Example with managed roles `Developer, Designer, HR`:

| Sheet | Member has | Result |
|---|---|---|
| `Developer` | `Member` | add `Developer` |
| `Developer, Designer` | `Developer` | add `Designer` |
| `Designer` | `Developer, Designer` | remove `Developer` |
| `Designer` | `Admin, Moderator, Developer, VIP` | add `Designer`, remove `Developer`; `Admin`, `Moderator`, `VIP` untouched |

### ⚠️ Empty role cells remove managed roles

If a member has a valid, uniquely identified row with an **empty ROLE cell**, that
means "this person should have **no managed roles**": the bot removes their
existing managed roles. Unmanaged roles (`Admin`, `VIP`, …) are still preserved.
Run `/preview-sync` first when clearing cells. A *malformed* row is never treated
this way (see below).

### Row-level rules (a bad row or role is skipped; everything else still proceeds)

| Situation | Behaviour |
|---|---|
| Username not in server / Discord ID not in server | Skipped and reported; nobody else is matched. |
| `DISCORD ID` present | ID is used; the username is **not** used as fallback. |
| Invalid Discord ID | Skipped and reported. |
| No username and no ID | Skipped and reported. |
| Duplicate username or duplicate ID (or several rows resolving to the same member) | **All** involved rows skipped as ambiguous. |
| A listed role does not exist in Discord | **That role is skipped and reported**; the row's other roles are still applied. |
| A listed role exists but is **not** in `MANAGED_ROLES` | That role is skipped and reported (so the sheet can never grant e.g. `Admin`). |
| Every listed role is invalid (e.g. a typo-only cell) | Row skipped, member **untouched**. It is *not* treated as an empty cell. |
| ROLE cell like `,,` (no valid names) | Skipped as malformed, not treated as empty. |
| Completely empty row | Ignored. |

Usernames are matched against the real account **username** (not nickname or display
name), ignoring case, surrounding whitespace and one leading `@`.
Role-name matching is case-insensitive.

### The report

The reply contains: members processed, roles added, roles removed, records skipped,
members unchanged, invalid roles skipped, errors and the **duration of the run**. For dry runs it says **DRY RUN: no changes were applied**
and reports *planned* counts instead. When there is detail (operations, skips,
errors) a `.txt` file is attached. Failed operations are never counted as successful.

---

## 9. Testing

```bash
pytest -v
```

The suite uses fakes and mocks only: **no Discord token, server, Google credentials
or network access are needed.** It covers role comparison, username normalization,
spreadsheet parsing, Google error handling, authorization, dry-run guarantees,
failure handling, concurrency, the discord.py adapter (with fake guild objects),
logging/redaction and command registration.

What the tests cannot prove: behaviour against a *live* Discord server or Google
Sheet. Do a `/preview-sync` in a test server first.

---

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Bot not online | Wrong token (`Discord rejected the bot token` in the console), or process not running. |
| Startup says Server Members Intent not enabled | Enable it in Developer Portal → Bot → Privileged Gateway Intents (§4). |
| Slash commands missing | Invite URL lacked `applications.commands`; wrong `DISCORD_GUILD_ID`; wait a minute / restart Discord. |
| "Incorrect bot token" | Re-copy the token or **Reset Token**; no quotes or spaces in `.env`. |
| `GOOGLE_SERVICE_ACCOUNT_FILE does not point to an existing file` | Put the JSON at that path; check the working directory. |
| "Google denied access" / "Spreadsheet not found" | Share the sheet with the service account's `client_email`; check `GOOGLE_SHEET_ID`; ensure the Sheets API is enabled. |
| "Worksheet 'X' was not found" | Fix `GOOGLE_WORKSHEET_NAME` to match the tab exactly. |
| "Missing required column(s)" | Row 1 needs `DISCORD USERNAME` and `ROLE`. |
| Row reported "Username not found" | The user changed their username, or typed a nickname. Use the current username, or fill `DISCORD ID`. The bot never guesses. |
| "Managed role '…' does not exist" | Create the role in Discord or fix `MANAGED_ROLES` (spelling). |
| "Bot cannot manage role '…'" / "Discord refused" | Move the bot's role above that role; ensure Manage Roles. Members whose top role outranks the bot cannot be modified. |
| "Does not have the Manage Roles permission" | Grant Manage Roles to the bot's role. |
| Sync takes long / rate limits | discord.py waits on Discord's rate limits automatically; large syncs take minutes. Don't re-run; check `logs/sync.log`. |
| "Failed" operations in the report | Read the reason per operation. The sync may be **partial**; fix the cause and re-run (it only changes what still differs). |
| Discord IDs rejected as invalid | The ID column was a number format; set to Plain text and re-enter. |

---

## 11. Security notes and known limitations

**Secrets**
- The token is read from `.env`; the service-account key is an external file. Both
  are git-ignored. Do not paste secrets into source code or chat.
- The bot never prints the token, keys or full credential objects. Startup logs a
  sanitized summary only, and a log filter masks the token if it ever appears.
- The Google scope is read-only.

**Privacy**
- Replies to commands are ephemeral. Detailed reports are only sent to the person who
  ran the command. The optional log channel receives counts only, never names.
- `logs/sync.log` records who ran what, plus member usernames/IDs and role changes
  (audit trail). Restrict access to that file; it rotates at 5 MB.

**Known limitations**
- **Not transactional.** Discord role changes are individual API calls; if one fails
  mid-run the server can be partially updated. The report lists exactly what failed;
  re-running converges.
- **Usernames change.** A stale username in the sheet is reported, not guessed. Prefer
  the `DISCORD ID` column for stability.
- **Concurrency lock is per process.** It prevents overlapping syncs inside one bot
  process only; do not run two bot processes against the same server.
- **Role hierarchy and managed (integration) roles** are enforced by Discord; the bot
  reports them as errors.
- **Single server.** One bot instance serves the `DISCORD_GUILD_ID` only.
- **Role names** are matched case-insensitively; two server roles that differ only by
  case make that managed role "ambiguous" and abort the sync.
- Member list is loaded from the Discord API on each sync (required for servers
  with 300+ members). Very large servers make syncs slower.
- Scheduled sync is a real (not dry-run) sync; the first one runs one interval after
  startup. Join sync only **adds** roles for the joining member.
- Sheet edits made *during* a sync are not seen until the next run (the sheet is read
  once, up front).

### Design assumptions (where the brief was open)
- An invalid role is skipped on its own (as the specification says: "Skip that role,
  report it"). One consequence: with a typo such as `Developer, Desginer`, the valid
  `Developer` is applied and any managed role not validly listed (e.g. the member's
  existing `Designer`) is removed. Always run `/preview-sync` first. As a guard, a cell
  whose roles are *all* invalid leaves the member untouched instead of clearing their
  managed roles.
- A missing Manage Roles permission aborts a real sync but only warns in a preview;
  a managed role the bot can't reach because of hierarchy blocks only operations on
  that role, and everything independent continues.
- `members_processed` counts valid, uniquely matched members (changed + unchanged).

---

## 12. Optional: CI

`.github/workflows/tests.yml` runs `pytest -v` on pushes and pull requests
(Python 3.11 and 3.12). It needs no secrets.

---

## Project layout

```
discord-hr-bot/
├── bot/
│   ├── main.py             # entry point, intents, scheduled/join jobs
│   ├── commands.py         # slash commands + handlers
│   ├── sheets.py           # Google Sheets read, parse, validate
│   ├── sync.py             # comparison, planning, SyncService (shared by every path)
│   ├── discord_gateway.py  # discord.py adapter (members, roles, errors)
│   ├── permissions.py      # HR/administrator authorization
│   ├── reporting.py        # summaries and .txt reports
│   ├── config.py           # env loading + validation
│   └── logging_config.py   # console + audit log, secret redaction
├── tests/                  # pytest suite (mocks only)
├── sample_data/sample_members.csv
├── .env.example  .gitignore  requirements.txt  pytest.ini
├── .github/workflows/tests.yml
└── logs/.gitkeep
```
