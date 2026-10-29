from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, NamedTuple, Protocol, Sequence

from bot.logging_config import AUDIT_LOGGER_NAME, audit_event
from bot.sheets import MemberRecord, SheetData, SheetsError, normalize_username

logger = logging.getLogger("bot.sync")

ACTION_ADD = "add"
ACTION_REMOVE = "remove"
STATUS_APPLIED = "applied"
STATUS_PLANNED = "planned"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"


class FatalSyncError(Exception):
    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class SyncBusyError(Exception):
    pass


class RoleOperationError(Exception):
    pass


def role_key(name: str) -> str:
    return name.strip().casefold()


class RoleChanges(NamedTuple):
    to_add: frozenset[str]
    to_remove: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not self.to_add and not self.to_remove


def calculate_role_changes(
    current_roles: Iterable[str],
    desired_roles: Iterable[str],
    managed_roles: Iterable[str],
) -> RoleChanges:
    managed = {role_key(r): r.strip() for r in managed_roles if r and r.strip()}
    if not managed:
        return RoleChanges(frozenset(), frozenset())

    current = {role_key(r) for r in current_roles} & managed.keys()
    desired = {role_key(r) for r in desired_roles} & managed.keys()
    return RoleChanges(
        to_add=frozenset(managed[k] for k in desired - current),
        to_remove=frozenset(managed[k] for k in current - desired),
    )


@dataclass(frozen=True)
class MemberSnapshot:
    id: int
    username: str
    role_names: frozenset[str]

    @property
    def label(self) -> str:
        return f"{self.username} ({self.id})"


@dataclass(frozen=True)
class RoleInfo:
    name: str
    id: int
    problem: str | None = None


@dataclass(frozen=True)
class SkippedRecord:
    row_numbers: tuple[int, ...]
    identifier: str
    reason: str


@dataclass(frozen=True)
class SkippedRole:
    row_number: int
    identifier: str
    role: str
    reason: str


@dataclass(frozen=True)
class MemberPlan:
    member: MemberSnapshot
    row_number: int
    desired: frozenset[str]
    changes: RoleChanges


@dataclass
class SyncPlan:
    member_plans: list[MemberPlan] = field(default_factory=list)
    unchanged: int = 0
    skipped: list[SkippedRecord] = field(default_factory=list)
    skipped_roles: list[SkippedRole] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return len(self.member_plans) + self.unchanged


@dataclass(frozen=True)
class OperationResult:
    member_label: str
    member_id: int
    role: str
    action: str
    status: str
    detail: str = ""


@dataclass
class SyncReport:
    dry_run: bool
    initiated_by: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    members_processed: int = 0
    members_unchanged: int = 0
    members_skipped: int = 0
    roles_added: int = 0
    roles_removed: int = 0
    planned_additions: int = 0
    planned_removals: int = 0
    sheet_empty_rows: int = 0
    duration_seconds: float = 0.0
    operations: list[OperationResult] = field(default_factory=list)
    skipped: list[SkippedRecord] = field(default_factory=list)
    skipped_roles: list[SkippedRole] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def failed_operations(self) -> int:
        return sum(1 for op in self.operations if op.status in (STATUS_FAILED, STATUS_BLOCKED))

    def as_dict(self) -> dict:
        return {
            "members_processed": self.members_processed,
            "roles_added": self.roles_added,
            "roles_removed": self.roles_removed,
            "planned_additions": self.planned_additions,
            "planned_removals": self.planned_removals,
            "members_skipped": self.members_skipped,
            "members_unchanged": self.members_unchanged,
            "roles_skipped": len(self.skipped_roles),
            "duration_seconds": round(self.duration_seconds, 2),
            "errors": list(self.errors),
            "dry_run": self.dry_run,
        }


@dataclass
class MemberInspection:
    member: MemberSnapshot
    record_found: bool
    desired: frozenset[str] = frozenset()
    current_managed: frozenset[str] = frozenset()
    changes: RoleChanges = RoleChanges(frozenset(), frozenset())
    skipped_reason: str | None = None
    skipped_roles: list[SkippedRole] = field(default_factory=list)


class SheetSource(Protocol):
    async def fetch_sheet_data(self) -> SheetData: ...


class GuildGateway(Protocol):
    guild_id: int

    @property
    def bot_can_manage_roles(self) -> bool: ...

    async def getMembers(self) -> Sequence[MemberSnapshot]: ...

    def listRoles(self) -> Sequence[RoleInfo]: ...

    async def addRole(self, member_id: int, role_name: str, reason: str) -> None: ...

    async def removeRole(self, member_id: int, role_name: str, reason: str) -> None: ...


def resolve_managed_roles(
    managed_names: Sequence[str], roles: Sequence[RoleInfo]
) -> tuple[list[RoleInfo], list[str]]:
    resolved: list[RoleInfo] = []
    problems: list[str] = []
    for name in managed_names:
        matches = [r for r in roles if role_key(r.name) == role_key(name)]
        if not matches:
            problems.append(f"Managed role '{name}' does not exist in this server.")
        elif len(matches) > 1:
            problems.append(
                f"Managed role '{name}' is ambiguous: {len(matches)} server roles share that name."
            )
        else:
            resolved.append(matches[0])
    return resolved, problems


def resolve_requested_roles(
    record: MemberRecord,
    managed_by_key: dict[str, str],
    guild_keys: set[str],
) -> tuple[frozenset[str], list[SkippedRole]]:
    desired: set[str] = set()
    skipped: list[SkippedRole] = []
    for role in record.roles:
        key = role_key(role)
        if key not in guild_keys:
            reason = "role does not exist in Discord"
        elif key not in managed_by_key:
            reason = "role is not in the managed-role allowlist"
        else:
            desired.add(managed_by_key[key])
            continue
        skipped.append(SkippedRole(record.row_number, record.identifier, role, reason))
    return frozenset(desired), skipped


def build_plan(
    records: Sequence[MemberRecord],
    members: Sequence[MemberSnapshot],
    managed_roles: Sequence[str],
    guild_role_names: Iterable[str],
) -> SyncPlan:
    managed_by_key = {role_key(r): r for r in managed_roles}
    guild_keys = {role_key(n) for n in guild_role_names}

    by_id = {m.id: m for m in members}
    by_username: dict[str, list[MemberSnapshot]] = defaultdict(list)
    for member in members:
        by_username[normalize_username(member.username)].append(member)

    plan = SyncPlan()
    resolved: dict[int, list[tuple[MemberRecord, MemberSnapshot]]] = defaultdict(list)

    for record in records:
        if record.discord_id is not None:
            member = by_id.get(record.discord_id)
            if member is None:
                plan.skipped.append(
                    SkippedRecord((record.row_number,), record.identifier, "Discord ID not found in this server")
                )
                continue
        else:
            candidates = by_username.get(record.username, [])
            if not candidates:
                plan.skipped.append(
                    SkippedRecord((record.row_number,), record.identifier, "Username not found in this server")
                )
                continue
            if len(candidates) > 1:
                plan.skipped.append(
                    SkippedRecord((record.row_number,), record.identifier, "Username matches several members (ambiguous)")
                )
                continue
            member = candidates[0]
        resolved[member.id].append((record, member))

    for pairs in resolved.values():
        member = pairs[0][1]
        if len(pairs) > 1:
            rows = tuple(rec.row_number for rec, _ in pairs)
            plan.skipped.append(
                SkippedRecord(
                    rows,
                    member.label,
                    f"Rows {', '.join(map(str, rows))} all refer to the same Discord member; skipped as ambiguous",
                )
            )
            continue

        record = pairs[0][0]
        desired, skipped_roles = resolve_requested_roles(record, managed_by_key, guild_keys)
        if record.roles and not desired:
            details = "; ".join(f"'{s.role}': {s.reason}" for s in skipped_roles)
            plan.skipped.append(
                SkippedRecord(
                    (record.row_number,),
                    record.identifier,
                    f"None of the listed roles are valid, member left untouched ({details})",
                )
            )
            continue
        plan.skipped_roles.extend(skipped_roles)

        changes = calculate_role_changes(member.role_names, desired, managed_roles)
        if changes.is_empty:
            plan.unchanged += 1
        else:
            plan.member_plans.append(MemberPlan(member, record.row_number, desired, changes))

    return plan


@dataclass
class _Preflight:
    sheet: SheetData
    managed: list[str]
    guild_role_names: list[str]
    blocked_roles: dict[str, str]
    warnings: list[str]


class SyncService:
    def __init__(
        self,
        sheet_source: SheetSource,
        managed_roles: Sequence[str],
        *,
        audit_logger: logging.Logger | None = None,
        operation_delay: float = 0.0,
    ) -> None:
        self._sheet_source = sheet_source
        self._managed_roles = tuple(managed_roles)
        self._operation_delay = max(0.0, operation_delay)
        self._lock = asyncio.Lock()
        self._audit = audit_logger or logging.getLogger(AUDIT_LOGGER_NAME)

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    async def run(self, gateway: GuildGateway, *, dry_run: bool, initiated_by: str) -> SyncReport:
        if self._lock.locked():
            raise SyncBusyError("A synchronisation is already running.")
        async with self._lock:
            return await self._run_locked(gateway, dry_run=dry_run, initiated_by=initiated_by)

    async def sync_new_member(
        self, gateway: GuildGateway, member: MemberSnapshot, *, initiated_by: str = "member_join"
    ) -> SyncReport:
        async with self._lock:
            report = SyncReport(dry_run=False, initiated_by=initiated_by)
            try:
                pre = await self._preflight(gateway, dry_run=False)
                report.warnings.extend(pre.warnings)
                plan = self._plan_for_member(pre, member)
            except FatalSyncError as exc:
                self._audit_event("sync_aborted", gateway, initiated_by, outcome="aborted", detail=str(exc))
                raise
            self._fill_counts(report, plan, pre)
            await self._execute(gateway, plan, pre, report, initiated_by, allow_removals=False)
            self._finish(report, gateway, initiated_by)
            return report

    async def inspect_member(self, gateway: GuildGateway, member: MemberSnapshot) -> MemberInspection:
        pre = await self._preflight(gateway, dry_run=True)
        matching = self._records_for_member(pre.sheet.records, member)
        if not matching:
            reason = None
            for issue in pre.sheet.issues:
                if issue.identifier.strip().lower() in {
                    normalize_username(member.username),
                    str(member.id),
                    "@" + normalize_username(member.username),
                }:
                    reason = f"Row {issue.row_number}: {issue.reason}"
                    break
            return MemberInspection(member, record_found=False, skipped_reason=reason)

        plan = build_plan(matching, [member], pre.managed, pre.guild_role_names)
        current = frozenset(r for r in pre.managed if role_key(r) in {role_key(x) for x in member.role_names})
        if plan.skipped:
            return MemberInspection(
                member, True, current_managed=current, skipped_reason=plan.skipped[0].reason
            )
        desired, skipped_roles = resolve_requested_roles(
            matching[0],
            {role_key(m): m for m in pre.managed},
            {role_key(n) for n in pre.guild_role_names},
        )
        changes = calculate_role_changes(member.role_names, desired, pre.managed)
        return MemberInspection(member, True, desired, current, changes, skipped_roles=skipped_roles)

    async def _run_locked(self, gateway: GuildGateway, *, dry_run: bool, initiated_by: str) -> SyncReport:
        self._audit_event("sync_started", gateway, initiated_by, outcome="dry_run" if dry_run else "live")
        report = SyncReport(dry_run=dry_run, initiated_by=initiated_by)
        try:
            pre = await self._preflight(gateway, dry_run=dry_run)
            members = await self._load_members(gateway)
            plan = build_plan(pre.sheet.records, members, pre.managed, pre.guild_role_names)
        except FatalSyncError as exc:
            self._audit_event("sync_aborted", gateway, initiated_by, outcome="aborted", detail=str(exc))
            raise

        report.warnings.extend(pre.warnings)
        self._fill_counts(report, plan, pre)
        await self._execute(gateway, plan, pre, report, initiated_by, allow_removals=True)
        self._finish(report, gateway, initiated_by)
        return report

    async def _preflight(self, gateway: GuildGateway, *, dry_run: bool) -> _Preflight:
        if not self._managed_roles:
            raise FatalSyncError(
                ["No managed roles are configured (MANAGED_ROLES is empty); nothing to synchronise."]
            )

        try:
            sheet = await self._sheet_source.fetch_sheet_data()
        except SheetsError as exc:
            raise FatalSyncError([f"Google Sheets: {exc}"]) from exc
        except Exception as exc:
            logger.exception("Unexpected failure while reading the sheet")
            raise FatalSyncError(["Unexpected error while reading the spreadsheet."]) from exc

        roles = list(gateway.listRoles())
        resolved, problems = resolve_managed_roles(self._managed_roles, roles)
        if problems:
            raise FatalSyncError(problems)

        warnings: list[str] = []
        if not gateway.bot_can_manage_roles:
            message = "The bot does not have the Manage Roles permission."
            if not dry_run:
                raise FatalSyncError([message])
            warnings.append(message + " A real sync would be refused.")

        blocked = {r.name: r.problem for r in resolved if r.problem}
        for name, why in blocked.items():
            warnings.append(f"Bot cannot manage role '{name}': {why}.")

        return _Preflight(
            sheet=sheet,
            managed=[r.name for r in resolved],
            guild_role_names=[r.name for r in roles],
            blocked_roles=blocked,
            warnings=warnings,
        )

    async def _load_members(self, gateway: GuildGateway) -> Sequence[MemberSnapshot]:
        try:
            members = await gateway.getMembers()
        except Exception as exc:
            logger.exception("Failed to load guild members")
            raise FatalSyncError(
                ["Could not load the server member list (is the Server Members Intent enabled?)."]
            ) from exc
        if not members:
            raise FatalSyncError(["The server member list came back empty; refusing to continue."])
        return members

    @staticmethod
    def _records_for_member(records: Sequence[MemberRecord], member: MemberSnapshot) -> list[MemberRecord]:
        name = normalize_username(member.username)
        return [
            r
            for r in records
            if r.discord_id == member.id or (r.discord_id is None and r.username == name)
        ]

    def _plan_for_member(self, pre: _Preflight, member: MemberSnapshot) -> SyncPlan:
        matching = self._records_for_member(pre.sheet.records, member)
        return build_plan(matching, [member], pre.managed, pre.guild_role_names)

    def _fill_counts(self, report: SyncReport, plan: SyncPlan, pre: _Preflight) -> None:
        report.members_processed = plan.processed
        report.members_unchanged = plan.unchanged
        report.sheet_empty_rows = pre.sheet.empty_rows
        sheet_skips = [
            SkippedRecord((i.row_number,), i.identifier, i.reason) for i in pre.sheet.issues
        ]
        report.skipped = sheet_skips + plan.skipped
        report.skipped_roles = list(plan.skipped_roles)
        report.members_skipped = len(report.skipped)

    async def _execute(
        self,
        gateway: GuildGateway,
        plan: SyncPlan,
        pre: _Preflight,
        report: SyncReport,
        initiated_by: str,
        *,
        allow_removals: bool,
    ) -> None:
        managed_keys = {role_key(m) for m in pre.managed}
        reason = f"HR role sync by {initiated_by}"

        for skipped in report.skipped:
            self._audit_event(
                "record_skipped", gateway, initiated_by,
                member=skipped.identifier, outcome="skipped", detail=skipped.reason,
            )

        for skipped_role in report.skipped_roles:
            self._audit_event(
                "role_skipped", gateway, initiated_by,
                member=skipped_role.identifier, role=skipped_role.role,
                outcome="skipped", detail=skipped_role.reason,
            )

        for member_plan in plan.member_plans:
            member = member_plan.member
            ops: list[tuple[str, str]] = [(ACTION_ADD, r) for r in sorted(member_plan.changes.to_add)]
            if allow_removals:
                ops += [(ACTION_REMOVE, r) for r in sorted(member_plan.changes.to_remove)]

            for action, role in ops:
                if role_key(role) not in managed_keys:
                    self._record(report, gateway, initiated_by, member, role, action, STATUS_FAILED,
                                 "Refused: role is not in the managed allowlist")
                    continue

                blocked_reason = pre.blocked_roles.get(role)
                if blocked_reason:
                    self._record(report, gateway, initiated_by, member, role, action, STATUS_BLOCKED,
                                 f"Bot cannot manage this role: {blocked_reason}")
                    continue

                if report.dry_run:
                    self._record(report, gateway, initiated_by, member, role, action, STATUS_PLANNED)
                    continue

                try:
                    if action == ACTION_ADD:
                        await gateway.addRole(member.id, role, reason)
                    else:
                        await gateway.removeRole(member.id, role, reason)
                except RoleOperationError as exc:
                    self._record(report, gateway, initiated_by, member, role, action, STATUS_FAILED, str(exc))
                except Exception as exc:
                    logger.exception("Unexpected error applying role change")
                    self._record(report, gateway, initiated_by, member, role, action, STATUS_FAILED,
                                 f"Unexpected error ({type(exc).__name__})")
                else:
                    self._record(report, gateway, initiated_by, member, role, action, STATUS_APPLIED)
                if self._operation_delay:
                    await asyncio.sleep(self._operation_delay)

    def _record(
        self,
        report: SyncReport,
        gateway: GuildGateway,
        initiated_by: str,
        member: MemberSnapshot,
        role: str,
        action: str,
        status: str,
        detail: str = "",
    ) -> None:
        report.operations.append(OperationResult(member.label, member.id, role, action, status, detail))
        if status == STATUS_APPLIED:
            if action == ACTION_ADD:
                report.roles_added += 1
            else:
                report.roles_removed += 1
        elif status == STATUS_PLANNED:
            if action == ACTION_ADD:
                report.planned_additions += 1
            else:
                report.planned_removals += 1
        else:
            verb = "add" if action == ACTION_ADD else "remove"
            prefix = "(dry run, would fail) " if report.dry_run else ""
            report.errors.append(f"{prefix}Could not {verb} role '{role}' for {member.label}: {detail}")

        self._audit_event(
            f"role_{action}", gateway, initiated_by,
            member=member.label, role=role, outcome=status, detail=detail,
        )

    def _finish(self, report: SyncReport, gateway: GuildGateway, initiated_by: str) -> None:
        report.duration_seconds = (datetime.now(timezone.utc) - report.started_at).total_seconds()
        self._audit_event(
            "sync_finished", gateway, initiated_by,
            outcome="dry_run" if report.dry_run else "live",
            detail=(
                f"processed={report.members_processed} added={report.roles_added} "
                f"removed={report.roles_removed} planned_add={report.planned_additions} "
                f"planned_remove={report.planned_removals} skipped={report.members_skipped} skipped_roles={len(report.skipped_roles)} "
                f"unchanged={report.members_unchanged} errors={len(report.errors)} "
                f"duration={report.duration_seconds:.2f}s"
            ),
        )

    def _audit_event(
        self, event: str, gateway: GuildGateway, initiated_by: str, **fields: object
    ) -> None:
        audit_event(self._audit, event, initiator=initiated_by, guild=gateway.guild_id, **fields)
