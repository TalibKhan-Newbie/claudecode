"""Tests for the field gate — the "any 2 of name/phone/address/email" rule."""

from __future__ import annotations

import pytest

from creatorcontacts.models import (
    ContactKind,
    ContactPoint,
    Creator,
    Evidence,
    Platform,
    SourceType,
)
from creatorcontacts.score import GateConfig, evaluate, lead_score, partition, resolve_name


def ev(source_type=SourceType.CONTACT_PAGE):
    return Evidence(source_url="https://site.in/contact", source_type=source_type)


def email(addr="a@b.in", conf=0.8, business=True):
    return ContactPoint(
        kind=ContactKind.EMAIL,
        value=addr,
        normalized=addr,
        confidence=conf,
        is_business=business,
        evidence=ev(),
    )


def phone(num="+919876543210", conf=0.85):
    return ContactPoint(
        kind=ContactKind.PHONE,
        value=num,
        normalized=num,
        confidence=conf,
        is_business=True,
        evidence=ev(SourceType.TEL_LINK),
    )


def address(line="21 Link Road, Mumbai 400053"):
    return ContactPoint(
        kind=ContactKind.ADDRESS,
        value=line,
        normalized=line,
        confidence=0.85,
        is_business=True,
        evidence=ev(SourceType.SCHEMA_ORG),
    )


def social(url="https://instagram.com/x"):
    return ContactPoint(
        kind=ContactKind.SOCIAL,
        value=url,
        normalized=f"instagram:{url[-1]}",
        confidence=0.6,
        evidence=ev(SourceType.PROFILE_BIO),
    )


def creator(name="Rahul", native="rahul", followers=55_000, contacts=()):
    made = Creator(
        platform=Platform.YOUTUBE,
        native_id=native,
        display_name=name,
        follower_count=followers,
        links=["https://site.in"],
    )
    for point in contacts:
        made.add_contact(point)
    return made


DEFAULT = GateConfig(min_fields=2, require_reachable=True)


def test_name_alone_fails():
    result = evaluate(creator(), DEFAULT)
    assert not result.passed
    assert "email or phone" in result.reason


def test_name_plus_email_passes():
    result = evaluate(creator(contacts=[email()]), DEFAULT)
    assert result.passed
    assert result.field_count == 2
    assert result.reachable


def test_name_plus_social_fails_when_reachability_required():
    assert not evaluate(creator(contacts=[social()]), DEFAULT).passed


def test_name_plus_social_passes_under_literal_rule():
    """--allow-unreachable gives the literal 'any 2 fields' reading."""
    loose = GateConfig(min_fields=2, require_reachable=False)
    assert evaluate(creator(contacts=[social()]), loose).passed


def test_email_and_phone_and_name_is_three_fields():
    result = evaluate(creator(contacts=[email(), phone()]), DEFAULT)
    assert result.field_count == 3
    assert set(result.kinds) == {"email", "phone"}


def test_address_counts_as_a_field():
    result = evaluate(creator(contacts=[email(), address()]), DEFAULT)
    assert result.field_count == 3
    assert "address" in result.kinds


def test_low_confidence_contacts_are_ignored():
    result = evaluate(creator(contacts=[email(conf=0.2)]), DEFAULT)
    assert not result.passed


def test_min_confidence_is_configurable():
    lenient = GateConfig(min_fields=2, min_confidence=0.1)
    assert evaluate(creator(contacts=[email(conf=0.2)]), lenient).passed


def test_require_business_email_drops_personal_addresses():
    strict = GateConfig(min_fields=2, require_business_email=True)
    personal = creator(contacts=[email("me@gmail.com", business=False)])
    assert not evaluate(personal, strict).passed
    assert evaluate(personal, DEFAULT).passed


def test_require_business_email_keeps_a_business_one():
    strict = GateConfig(min_fields=2, require_business_email=True)
    assert evaluate(creator(contacts=[email(business=True)]), strict).passed


def test_count_name_as_field_can_be_turned_off():
    literal = GateConfig(min_fields=2, count_name_as_field=False)
    # Email alone is now one field, not two.
    assert not evaluate(creator(contacts=[email()]), literal).passed
    assert evaluate(creator(contacts=[email(), phone()]), literal).passed


def test_min_fields_of_three_needs_more():
    strict = GateConfig(min_fields=3, require_reachable=True)
    assert not evaluate(creator(contacts=[email()]), strict).passed
    assert evaluate(creator(contacts=[email(), phone()]), strict).passed


def test_resolve_name_uses_display_name_then_handle():
    assert resolve_name(creator(name="Rahul Comedy")) == "Rahul Comedy"
    nameless = Creator(platform=Platform.YOUTUBE, native_id="x", handle="@onlyhandle")
    assert resolve_name(nameless) == "@onlyhandle"
    assert resolve_name(Creator(platform=Platform.YOUTUBE, native_id="x")) == ""


def test_lead_score_prefers_business_email_and_more_channels():
    rich = creator(contacts=[email(business=True), phone(), address(), social()])
    poor = creator(contacts=[email(conf=0.5, business=False)])
    assert lead_score(rich) > lead_score(poor)


def test_lead_score_penalises_obfuscated_email():
    plain = creator(contacts=[email()])
    hidden_point = email()
    hidden_point.was_obfuscated = True
    hidden = creator(contacts=[hidden_point])
    assert lead_score(hidden) < lead_score(plain)


@pytest.mark.parametrize("contacts", [(), (social(),), (email(conf=0.1),)])
def test_lead_score_stays_in_range(contacts):
    assert 0 <= lead_score(creator(contacts=contacts)) <= 100


def test_partition_splits_and_ranks():
    good = creator("Good", "g", contacts=[email(), phone()])
    bad = creator("Bad", "b")
    passed, failed = partition([bad, good], DEFAULT)
    assert [c.display_name for c, _ in passed] == ["Good"]
    assert [c.display_name for c, _ in failed] == ["Bad"]


def test_partition_orders_passing_by_lead_score():
    strong = creator("Strong", "s", contacts=[email(business=True), phone(), address()])
    weak = creator("Weak", "w", contacts=[email(conf=0.55, business=False)])
    passed, _ = partition([weak, strong], DEFAULT)
    assert [c.display_name for c, _ in passed] == ["Strong", "Weak"]
