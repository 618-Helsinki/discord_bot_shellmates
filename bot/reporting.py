from __future__ import annotations

from bot.sync import (
    ACTION_ADD,
    STATUS_APPLIED,
    MemberInspection,
    SyncReport,
)

DISCORD_MESSAGE_LIMIT = 2000
_SAFE_LIMIT = 1900


def truncate(text: str, limit: int = _SAFE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 30].rstrip() + "\n… (truncated)"


def format_summary(report: SyncReport) -> str:
    if report.dry_run:
        lines = [
            "DRY RUN (no changes).",
            "These are *planned* operations only.",
            "",
            f"Members that will change: {report.members_processed - report.members_unchanged}",
            f"Roles that will be added: {report.planned_additions}",
            f"Roles that will be removed: {report.planned_removals}",
        ]
    else:
        lines = [
            "Role sync done." if not report.errors
            else "Role sync done with errors.",
            "",
            f"Roles added: {report.roles_added}",
            f"Roles removed: {report.roles_removed}",
        ]
    lines += [
        f"Members modified: {report.members_processed}",
        f"Members unchanged: {report.members_unchanged}",
        f"Records skipped: {report.members_skipped}",
        *([f"Invalid roles skipped: {len(report.skipped_roles)}"] if report.skipped_roles else []),
        f"Errors: {len(report.errors)}",
        f"Duration: {report.duration_seconds:.1f}s",
    ]
    if report.warnings:
        lines += ["", "Warnings"] + [f"• {w}" for w in report.warnings[:5]]
        if len(report.warnings) > 5:
            lines.append(f"• …and {len(report.warnings) - 5} more (see attached report)")
    if report.errors:
        lines += ["", "First errors"] + [f"• {e}" for e in report.errors[:3]]
        if len(report.errors) > 3:
            lines.append(f"• …and {len(report.errors) - 3} more (see attached report)")
    if not report.dry_run and report.errors:
        lines += ["", "Some operations failed, so the server may be partially updated."]
    return truncate("\n".join(lines))


def has_details(report: SyncReport) -> bool:
    return bool(
        report.operations or report.skipped or report.skipped_roles or report.errors or report.warnings
    )


def format_detailed_report(report: SyncReport) -> str:
    mode = "DRY RUN (no changes applied)" if report.dry_run else "LIVE SYNC"
    out = [
        f"Role synchronization report: {mode}",
        f"Started (local): {report.started_at.astimezone():%Y-%m-%d %H:%M:%S %Z (%z)}",
        f"Initiated by: {report.initiated_by}",
        "",
        "--------------------SUMMARY---------------------",
        f"Members processed : {report.members_processed}",
        f"Members unchanged : {report.members_unchanged}",
        f"Records skipped   : {report.members_skipped}",
        f"Roles skipped     : {len(report.skipped_roles)}",
        f"Empty sheet rows  : {report.sheet_empty_rows}",
        f"Duration          : {report.duration_seconds:.1f} s",
    ]
    if report.dry_run:
        out += [
            f"Planned additions : {report.planned_additions}",
            f"Planned removals  : {report.planned_removals}",
        ]
    else:
        out += [f"Roles added       : {report.roles_added}", f"  Roles removed     : {report.roles_removed}"]
    out.append(f"Errors            : {len(report.errors)}")

    if report.warnings:
        out += ["", "WARNINGS"] + [f"  - {w}" for w in report.warnings]

    if report.operations:
        out += ["", "OPERATIONS", "  member | action | role | status | detail"]
        for op in report.operations:
            verb = "ADD" if op.action == ACTION_ADD else "REMOVE"
            status = op.status.upper()
            detail = f" | {op.detail}" if op.detail else ""
            out.append(f"  {op.member_label} | {verb} | {op.role} | {status}{detail}")

    if report.skipped:
        out += ["", "SKIPPED RECORDS"]
        for skip in report.skipped:
            rows = ", ".join(map(str, skip.row_numbers))
            out.append(f"  row {rows} | {skip.identifier} | {skip.reason}")

    if report.skipped_roles:
        out += ["", "SKIPPED ROLES (invalid role ignored, rest was applied)"]
        for item in report.skipped_roles:
            out.append(f"  row {item.row_number} | {item.identifier} | {item.role} | {item.reason}")

    if report.errors:
        out += ["", "ERRORS"] + [f"  - {e}" for e in report.errors]

    if not report.dry_run and any(op.status != STATUS_APPLIED for op in report.operations):
        out += ["", "NOTE: Discord changes are not transactional; this sync may have partially completed."]
    return "\n".join(out) + "\n"


def format_log_channel_summary(report: SyncReport) -> str:
    mode = "DRY RUN" if report.dry_run else "SYNC"
    return (
        f"Role {mode} by {report.initiated_by}: "
        f"added {report.roles_added}, removed {report.roles_removed}, "
        f"planned +{report.planned_additions}/-{report.planned_removals}, "
        f"skipped {report.members_skipped}, errors {len(report.errors)}."
    )


def format_inspection(inspection: MemberInspection) -> str:
    m = inspection.member
    if not inspection.record_found:
        extra = f"\nReason: {inspection.skipped_reason}" if inspection.skipped_reason else ""
        return f"{m.username} has no valid record in the spreadsheet.{extra}"
    current = ", ".join(sorted(inspection.current_managed)) or "none"
    if inspection.skipped_reason:
        return (
            f"{m.username}: the spreadsheet record is invalid and would be skipped.\n"
            f"Reason: {inspection.skipped_reason}\nCurrent managed roles: {current}"
        )
    desired = ", ".join(sorted(inspection.desired)) or "none"
    lines = [
        f"{m.username}",
        f"Sheet roles: {desired}",
        f"Current managed roles: {current}",
    ]
    for item in inspection.skipped_roles:
        lines.append(f"Role '{item.role}' would be skipped: {item.reason}")
    if inspection.changes.is_empty:
        lines.append("In sync: nothing to change.")
    else:
        if inspection.changes.to_add:
            lines.append("Would add: " + ", ".join(sorted(inspection.changes.to_add)))
        if inspection.changes.to_remove:
            lines.append("Would remove: " + ", ".join(sorted(inspection.changes.to_remove)))
        lines.append("(Nothing was changed. Run /sync-roles to apply.)")
    return truncate("\n".join(lines))
