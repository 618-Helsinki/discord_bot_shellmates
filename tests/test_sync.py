import asyncio

import pytest

from bot.sheets import MemberRecord, SheetsError, parse_sheet
from bot.sync import (
    FatalSyncError,
    SyncBusyError,
    SyncService,
    build_plan,
    calculate_role_changes,
    resolve_managed_roles,
)
from tests.fakes import (
    FakeGateway,
    FakeSheetSource,
    SlowSheetSource,
    member,
    role_infos,
    run,
)

MANAGED = ["Developer", "Designer", "HR"]
ALL_ROLES = ["Admin", "Moderator", "Developer", "Designer", "HR", "VIP", "Content", "Member"]


def changes(current, desired, managed=MANAGED):
    c = calculate_role_changes(current, desired, managed)
    return set(c.to_add), set(c.to_remove)


def test_adds_missing_managed_role():
    assert changes({"Member"}, {"Developer"}) == ({"Developer"}, set())


def test_adds_second_role():
    assert changes({"Developer"}, {"Developer", "Designer"}) == ({"Designer"}, set())


def test_removes_outdated_managed_role():
    assert changes({"Developer", "Designer"}, {"Designer"}) == (set(), {"Developer"})


def test_add_and_remove_together_preserving_unmanaged():
    add, remove = changes({"Admin", "Moderator", "Developer", "VIP"}, {"Designer"})
    assert add == {"Designer"}
    assert remove == {"Developer"}
    assert not ({"Admin", "Moderator", "VIP"} & (add | remove))


def test_empty_desired_removes_only_managed():
    add, remove = changes({"Admin", "Developer", "HR", "VIP"}, set())
    assert add == set()
    assert remove == {"Developer", "HR"}


def test_no_existing_managed_roles():
    assert changes({"Admin", "VIP"}, {"HR"}) == ({"HR"}, set())


def test_already_in_sync_is_empty():
    result = calculate_role_changes({"Developer", "Admin"}, {"Developer"}, MANAGED)
    assert result.is_empty


def test_empty_managed_config_does_nothing():
    assert changes({"Developer", "Admin"}, {"HR"}, managed=[]) == (set(), set())


def test_unmanaged_desired_role_is_never_added():
    add, remove = changes({"Member"}, {"Admin", "Developer"})
    assert add == {"Developer"}
    assert "Admin" not in add | remove


def test_duplicate_desired_roles_ignored():
    result = calculate_role_changes([], ["Developer", "Developer", "developer"], MANAGED)
    assert set(result.to_add) == {"Developer"}


def test_matching_is_case_insensitive_and_uses_managed_spelling():
    add, remove = changes({"designer"}, {"DEVELOPER"})
    assert add == {"Developer"} and remove == {"Designer"}


def test_inputs_are_not_mutated():
    current, desired, managed = {"Developer", "Admin"}, {"HR"}, ["Developer", "HR"]
    snapshot = (set(current), set(desired), list(managed))
    calculate_role_changes(current, desired, managed)
    assert (current, desired, managed) == snapshot


def rec(row, user, roles, discord_id=None):
    return MemberRecord(row, user, user.lower(), discord_id, tuple(roles))


def plan_for(records, members, managed=MANAGED, roles=ALL_ROLES):
    return build_plan(records, members, managed, roles)


def test_member_not_found_is_skipped_and_reported():
    plan = plan_for([rec(2, "old_username", ["Developer"])], [member(1, "someone")])
    assert not plan.member_plans
    assert plan.skipped[0].identifier == "old_username"
    assert "not found" in plan.skipped[0].reason


def test_unknown_role_is_skipped_and_reported_but_valid_roles_still_apply():
    m = member(1, "member1", "Designer")
    plan = plan_for([rec(2, "member1", ["Developer", "Desginer"])], [m])
    assert plan.skipped == []
    assert plan.member_plans[0].changes.to_add == {"Developer"}
    assert plan.member_plans[0].changes.to_remove == {"Designer"}
    assert [(s.role, s.reason) for s in plan.skipped_roles] == [("Desginer", "role does not exist in Discord")]


def test_row_with_only_invalid_roles_leaves_member_untouched():
    m = member(1, "member1", "Developer", "HR")
    plan = plan_for([rec(2, "member1", ["Desginer"])], [m])
    assert not plan.member_plans and plan.unchanged == 0 and plan.skipped_roles == []
    assert "None of the listed roles are valid" in plan.skipped[0].reason


def test_existing_but_unmanaged_requested_role_is_never_granted():
    plan = plan_for([rec(2, "member1", ["Admin"])], [member(1, "member1")])
    assert not plan.member_plans
    assert "not in the managed-role allowlist" in plan.skipped[0].reason
    plan = plan_for([rec(2, "member1", ["Admin", "Developer"])], [member(1, "member1")])
    assert plan.member_plans[0].changes.to_add == {"Developer"}
    assert plan.skipped_roles[0].role == "Admin"


def test_discord_id_is_preferred_and_never_falls_back_to_username():
    members = [member(111, "alice"), member(222, "bob")]
    plan = plan_for([rec(2, "alice", ["HR"], discord_id=999)], members)
    assert not plan.member_plans
    assert "ID not found" in plan.skipped[0].reason
    plan = plan_for([rec(2, "alice", ["HR"], discord_id=222)], members)
    assert plan.member_plans[0].member.id == 222


def test_rows_resolving_to_same_member_are_skipped():
    members = [member(111, "alice")]
    records = [rec(2, "alice", ["HR"]), rec(3, "x", ["Developer"], discord_id=111)]
    plan = plan_for(records, members)
    assert not plan.member_plans
    assert plan.skipped[0].row_numbers == (2, 3)


def test_ambiguous_username_among_members_is_skipped():
    plan = plan_for([rec(2, "dup", ["HR"])], [member(1, "dup"), member(2, "DUP")])
    assert not plan.member_plans and "ambiguous" in plan.skipped[0].reason


def test_username_matching_ignores_case_and_at_sign_via_sheet_parser():
    data = parse_sheet([["DISCORD USERNAME", "ROLE"], [" @Member1 ", "Developer"]])
    plan = plan_for(data.records, [member(1, "member1")])
    assert plan.member_plans[0].changes.to_add == {"Developer"}


def test_empty_role_cell_removes_managed_but_keeps_unmanaged():
    data = parse_sheet([["DISCORD USERNAME", "ROLE"], ["member1", ""]])
    m = member(1, "member1", "Admin", "Developer", "HR", "VIP")
    plan = plan_for(data.records, [m])
    assert plan.member_plans[0].changes.to_remove == {"Developer", "HR"}
    assert plan.member_plans[0].changes.to_add == frozenset()


def test_resolve_managed_roles_reports_missing_and_ambiguous():
    roles = role_infos("Developer", "HR", "hr")
    resolved, problems = resolve_managed_roles(["Developer", "Designer", "HR"], roles)
    assert [r.name for r in resolved] == ["Developer"]
    assert any("Designer" in p and "does not exist" in p for p in problems)
    assert any("HR" in p and "ambiguous" in p for p in problems)


def sheet(*rows):
    return parse_sheet([["DISCORD USERNAME", "ROLE", "DISCORD ID"], *[list(r) for r in rows]])


def make_service(data=None, error=None, managed=MANAGED):
    return SyncService(FakeSheetSource(data, error), managed)


def test_live_sync_applies_changes_and_counts_them():
    svc = make_service(sheet(("m1", "Developer, Designer", ""), ("m2", "HR", "")))
    gw = FakeGateway(
        [member(1, "m1", "Member", "HR"), member(2, "m2", "HR", "Admin")],
        role_infos(*ALL_ROLES),
    )
    report = run(svc.run(gw, dry_run=False, initiated_by="tester"))
    assert sorted(gw.calls) == sorted(
        [("add", 1, "Developer"), ("add", 1, "Designer"), ("remove", 1, "HR")]
    )
    assert (report.roles_added, report.roles_removed) == (2, 1)
    assert report.members_processed == 2 and report.members_unchanged == 1
    assert report.errors == [] and not report.dry_run


def test_dry_run_makes_no_mutations_and_reports_plan():
    svc = make_service(sheet(("m1", "Developer", "")))
    gw = FakeGateway([member(1, "m1", "HR")], role_infos(*ALL_ROLES))
    report = run(svc.run(gw, dry_run=True, initiated_by="tester"))
    assert gw.calls == []
    assert report.dry_run
    assert (report.planned_additions, report.planned_removals) == (1, 1)
    assert (report.roles_added, report.roles_removed) == (0, 0)


def test_sheet_failure_causes_zero_mutations():
    svc = make_service(error=SheetsError("boom"))
    gw = FakeGateway([member(1, "m1")], role_infos(*ALL_ROLES))
    with pytest.raises(FatalSyncError):
        run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert gw.calls == []


def test_unexpected_sheet_exception_is_fatal_not_a_crash():
    svc = make_service(error=RuntimeError("weird"))
    gw = FakeGateway([member(1, "m1")], role_infos(*ALL_ROLES))
    with pytest.raises(FatalSyncError):
        run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert gw.calls == []


def test_missing_managed_role_in_discord_is_fatal():
    svc = make_service(sheet(("m1", "Developer", "")))
    gw = FakeGateway([member(1, "m1")], role_infos("Developer", "HR"))
    with pytest.raises(FatalSyncError) as exc:
        run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert "Designer" in str(exc.value)
    assert gw.calls == []


def test_empty_managed_config_is_fatal_and_changes_nothing():
    svc = make_service(sheet(("m1", "Developer", "")), managed=[])
    gw = FakeGateway([member(1, "m1")], role_infos(*ALL_ROLES))
    with pytest.raises(FatalSyncError) as exc:
        run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert "No managed roles" in str(exc.value)
    assert gw.calls == []


def test_missing_manage_roles_permission_blocks_live_sync_but_not_preview():
    svc = make_service(sheet(("m1", "Developer", "")))
    gw = FakeGateway([member(1, "m1")], role_infos(*ALL_ROLES), can_manage_roles=False)
    with pytest.raises(FatalSyncError):
        run(svc.run(gw, dry_run=False, initiated_by="t"))
    report = run(svc.run(gw, dry_run=True, initiated_by="t"))
    assert gw.calls == [] and any("Manage Roles" in w for w in report.warnings)


def test_member_list_failure_is_fatal():
    svc = make_service(sheet(("m1", "Developer", "")))
    gw = FakeGateway([], role_infos(*ALL_ROLES), members_error=RuntimeError("intent"))
    with pytest.raises(FatalSyncError):
        run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert gw.calls == []


def test_empty_member_list_is_fatal():
    svc = make_service(sheet(("m1", "Developer", "")))
    gw = FakeGateway([], role_infos(*ALL_ROLES))
    with pytest.raises(FatalSyncError):
        run(svc.run(gw, dry_run=False, initiated_by="t"))


def test_invalid_records_skipped_while_valid_ones_proceed():
    svc = make_service(
        sheet(
            ("m1", "Developer, UnknownRole", ""),
            ("ghost", "HR", ""),
            ("m2", "Designer", ""),
            ("", "HR", ""),
            ("m3", "HR", "not-an-id"),
        )
    )
    gw = FakeGateway(
        [member(1, "m1", "HR"), member(2, "m2")],
        role_infos(*ALL_ROLES),
    )
    report = run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert sorted(gw.calls) == [("add", 1, "Developer"), ("add", 2, "Designer"), ("remove", 1, "HR")]
    assert report.members_skipped == 3
    assert report.members_processed == 2
    assert [s.role for s in report.skipped_roles] == ["UnknownRole"]
    assert report.errors == []


def test_failed_operations_are_reported_and_not_counted_as_success():
    svc = make_service(sheet(("m1", "Developer, Designer", "")))
    gw = FakeGateway(
        [member(1, "m1")],
        role_infos(*ALL_ROLES),
        fail={(1, "Developer", "add"): "Discord refused: hierarchy"},
    )
    report = run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert report.roles_added == 1
    assert any("Developer" in e and "hierarchy" in e for e in report.errors)
    assert report.failed_operations == 1


def test_unexpected_exception_in_one_operation_does_not_abort_run():
    class Exploding(FakeGateway):
        async def addRole(self, member_id, role_name, reason):
            if role_name == "Developer":
                raise ValueError("kaboom")
            await super().addRole(member_id, role_name, reason)

    svc = make_service(sheet(("m1", "Developer, Designer", "")))
    gw = Exploding([member(1, "m1")], role_infos(*ALL_ROLES))
    report = run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert report.roles_added == 1 and len(report.errors) == 1


def test_blocked_role_is_reported_not_attempted_and_others_continue():
    svc = make_service(sheet(("m1", "Developer, Designer", "")))
    gw = FakeGateway(
        [member(1, "m1")],
        role_infos(*ALL_ROLES, blocked={"Developer": "role hierarchy"}),
    )
    report = run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert gw.calls == [("add", 1, "Designer")]
    assert report.roles_added == 1
    assert any("Developer" in e for e in report.errors)


def test_unmanaged_roles_never_reach_the_gateway():
    svc = make_service(sheet(("m1", "", "")))
    gw = FakeGateway(
        [member(1, "m1", "Admin", "Moderator", "VIP", "Developer")], role_infos(*ALL_ROLES)
    )
    run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert gw.calls == [("remove", 1, "Developer")]


def test_concurrent_sync_is_rejected():
    async def scenario():
        source = SlowSheetSource(sheet(("m1", "Developer", "")))
        svc = SyncService(source, MANAGED)
        gw = FakeGateway([member(1, "m1")], role_infos(*ALL_ROLES))
        first = asyncio.create_task(svc.run(gw, dry_run=True, initiated_by="a"))
        await asyncio.sleep(0.01)
        assert svc.is_running
        with pytest.raises(SyncBusyError):
            await svc.run(gw, dry_run=True, initiated_by="b")
        source.release.set()
        await first
        assert not svc.is_running

    run(scenario())


def test_lock_released_after_fatal_error():
    svc = make_service(error=SheetsError("x"))
    gw = FakeGateway([member(1, "m1")], role_infos(*ALL_ROLES))
    with pytest.raises(FatalSyncError):
        run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert not svc.is_running


def test_join_sync_only_adds_and_only_touches_that_member():
    svc = make_service(sheet(("new", "Developer", ""), ("other", "HR", "")))
    new = member(5, "new", "HR")
    gw = FakeGateway([new, member(6, "other")], role_infos(*ALL_ROLES))
    report = run(svc.sync_new_member(gw, new))
    assert gw.calls == [("add", 5, "Developer")]
    assert report.roles_added == 1 and report.roles_removed == 0


def test_join_sync_without_record_does_nothing():
    svc = make_service(sheet(("someone", "Developer", "")))
    new = member(5, "new")
    gw = FakeGateway([new], role_infos(*ALL_ROLES))
    run(svc.sync_new_member(gw, new))
    assert gw.calls == []


def test_join_sync_skips_invalid_role_but_grants_valid_one():
    svc = make_service(sheet(("new", "Developer, Nope", "")))
    new = member(5, "new")
    gw = FakeGateway([new], role_infos(*ALL_ROLES))
    report = run(svc.sync_new_member(gw, new))
    assert gw.calls == [("add", 5, "Developer")]
    assert [s.role for s in report.skipped_roles] == ["Nope"]


def test_join_sync_with_only_invalid_roles_grants_nothing():
    svc = make_service(sheet(("new", "Nope", "")))
    new = member(5, "new")
    gw = FakeGateway([new], role_infos(*ALL_ROLES))
    run(svc.sync_new_member(gw, new))
    assert gw.calls == []


def test_dry_run_reports_skipped_roles_without_mutating():
    svc = make_service(sheet(("m1", "Developer, Desginer", "")))
    gw = FakeGateway([member(1, "m1", "Designer")], role_infos(*ALL_ROLES))
    report = run(svc.run(gw, dry_run=True, initiated_by="t"))
    assert gw.calls == []
    assert (report.planned_additions, report.planned_removals) == (1, 1)
    assert [s.role for s in report.skipped_roles] == ["Desginer"]


def test_inspect_member_with_invalid_role_does_not_crash_and_reports_it():
    svc = make_service(sheet(("m1", "Developer, Desginer", "")))
    m = member(1, "m1")
    result = run(svc.inspect_member(FakeGateway([m], role_infos(*ALL_ROLES)), m))
    assert set(result.desired) == {"Developer"}
    assert [s.role for s in result.skipped_roles] == ["Desginer"]


def test_inspect_member_reports_differences_read_only():
    svc = make_service(sheet(("m1", "Designer", "")))
    m = member(1, "m1", "Developer", "Admin")
    gw = FakeGateway([m], role_infos(*ALL_ROLES))
    result = run(svc.inspect_member(gw, m))
    assert result.record_found
    assert set(result.changes.to_add) == {"Designer"}
    assert set(result.changes.to_remove) == {"Developer"}
    assert gw.calls == []


def test_inspect_member_without_record():
    svc = make_service(sheet(("other", "HR", "")))
    m = member(1, "m1")
    result = run(svc.inspect_member(FakeGateway([m], role_infos(*ALL_ROLES)), m))
    assert not result.record_found


def test_report_records_run_duration():
    svc = make_service(sheet(("m1", "Developer", "")))
    gw = FakeGateway([member(1, "m1")], role_infos(*ALL_ROLES))
    report = run(svc.run(gw, dry_run=True, initiated_by="t"))
    assert report.duration_seconds >= 0 and "duration_seconds" in report.as_dict()


def test_small_delay_between_real_updates_but_not_in_dry_run(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("bot.sync.asyncio.sleep", fake_sleep)
    data = sheet(("m1", "Developer, Designer", ""))
    svc = SyncService(FakeSheetSource(data), MANAGED, operation_delay=0.2)
    gw = FakeGateway([member(1, "m1")], role_infos(*ALL_ROLES))
    run(svc.run(gw, dry_run=True, initiated_by="t"))
    assert slept == []
    run(svc.run(gw, dry_run=False, initiated_by="t"))
    assert slept == [0.2, 0.2]
