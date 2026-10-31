from types import SimpleNamespace

from bot.permissions import (
    GUILD_ONLY_MESSAGE,
    UNAUTHORIZED_MESSAGE,
    check_interaction,
    is_authorized,
)
from tests.fakes import fake_interaction

HR = (111, 222)


def user(admin=False, role_ids=()):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=admin),
        roles=[SimpleNamespace(id=i) for i in role_ids],
    )


def test_hr_role_is_authorized():
    assert is_authorized(user(role_ids=[5, 222]), HR)


def test_administrator_is_authorized_even_without_hr_roles():
    assert is_authorized(user(admin=True), HR)
    assert is_authorized(user(admin=True), ())


def test_ordinary_member_is_denied():
    assert not is_authorized(user(role_ids=[5, 6]), HR)


def test_no_configured_hr_roles_means_admin_only():
    assert not is_authorized(user(role_ids=[111]), ())
    assert is_authorized(user(admin=True), ())


def test_plain_user_without_guild_attributes_is_denied():
    assert not is_authorized(SimpleNamespace(id=1, name="dm_user"), HR)
    assert not is_authorized(None, HR)


def test_role_names_do_not_grant_access():
    named = SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=False),
        roles=[SimpleNamespace(id=999, name="HR")],
    )
    assert not is_authorized(named, HR)


def test_check_interaction_allows_hr_in_guild():
    assert check_interaction(fake_interaction(role_ids=[111]), 1, HR).allowed


def test_check_interaction_denies_ordinary_member_politely():
    result = check_interaction(fake_interaction(role_ids=[5]), 1, HR)
    assert not result.allowed and result.message == UNAUTHORIZED_MESSAGE


def test_check_interaction_denies_outside_guild():
    result = check_interaction(fake_interaction(in_guild=False, admin=True), 1, HR)
    assert not result.allowed and result.message == GUILD_ONLY_MESSAGE


def test_check_interaction_denies_other_guild():
    assert not check_interaction(fake_interaction(guild_id=2, admin=True), 1, HR).allowed
