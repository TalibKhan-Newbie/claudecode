"""Tests for the email-domain filter and the target cap."""

from __future__ import annotations

import csv

import pytest

from creatorcontacts.export import to_csv
from creatorcontacts.models import (
    ContactKind,
    ContactPoint,
    Creator,
    Evidence,
    Platform,
    SourceType,
)
from creatorcontacts.score import (
    GMAIL_DOMAINS,
    GateConfig,
    allowed_emails,
    best_email,
    email_domain,
    evaluate,
    partition,
)

GMAIL = GateConfig(min_fields=2, require_reachable=True, allowed_email_domains=GMAIL_DOMAINS)
OPEN = GateConfig(min_fields=2, require_reachable=True)


def email(addr, conf=0.8, business=True):
    return ContactPoint(
        kind=ContactKind.EMAIL,
        value=addr,
        normalized=addr,
        confidence=conf,
        is_business=business,
        evidence=Evidence(source_url="https://s.in/contact", source_type=SourceType.CONTACT_PAGE),
    )


def phone(num="+919876543210"):
    return ContactPoint(
        kind=ContactKind.PHONE,
        value=num,
        normalized=num,
        confidence=0.85,
        is_business=True,
        evidence=Evidence(source_url="https://s.in/contact", source_type=SourceType.TEL_LINK),
    )


def creator(native="c1", name="Creator", followers=55_000, contacts=()):
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


# -- email_domain helper ----------------------------------------------------


@pytest.mark.parametrize(
    "address,expected",
    [
        ("a@gmail.com", "gmail.com"),
        ("A@GMAIL.COM", "gmail.com"),
        ("a.b+c@googlemail.com", "googlemail.com"),
        ("a@mail.studio.co.in", "mail.studio.co.in"),
        ("no-at-sign", ""),
    ],
)
def test_email_domain(address, expected):
    assert email_domain(address) == expected


# -- the gate ---------------------------------------------------------------


def test_gmail_address_passes_the_gmail_filter():
    assert evaluate(creator(contacts=[email("rahul@gmail.com")]), GMAIL).passed


def test_googlemail_is_treated_as_gmail():
    assert evaluate(creator(contacts=[email("rahul@googlemail.com")]), GMAIL).passed


def test_custom_domain_is_rejected_by_the_gmail_filter():
    result = evaluate(creator(contacts=[email("business@studiokaam.in")]), GMAIL)
    assert not result.passed
    assert "not on" in result.reason


def test_the_same_creator_passes_without_the_filter():
    subject = creator(contacts=[email("business@studiokaam.in")])
    assert evaluate(subject, OPEN).passed


def test_reject_reason_names_the_wanted_domains():
    result = evaluate(creator(contacts=[email("a@yahoo.com")]), GMAIL)
    assert "gmail.com" in result.reason


def test_domain_filter_does_not_touch_phone_or_address():
    """A creator reachable by phone still passes even with no Gmail address."""
    subject = creator(contacts=[email("a@studiokaam.in"), phone()])
    result = evaluate(subject, GMAIL)
    assert result.passed
    assert "phone" in result.kinds
    assert "email" not in result.kinds


def test_gmail_kept_when_creator_has_both_domains():
    subject = creator(contacts=[email("biz@studiokaam.in", conf=0.95), email("me@gmail.com", conf=0.6)])
    result = evaluate(subject, GMAIL)
    assert result.passed
    assert "email" in result.kinds


def test_empty_filter_allows_everything():
    for address in ("a@gmail.com", "a@studiokaam.in", "a@yahoo.co.in"):
        assert evaluate(creator(contacts=[email(address)]), OPEN).passed


def test_case_insensitive_matching():
    assert evaluate(creator(contacts=[email("Rahul@GMail.com")]), GMAIL).passed


def test_arbitrary_domain_list():
    only_in = GateConfig(min_fields=2, allowed_email_domains=frozenset({"studiokaam.in"}))
    assert evaluate(creator(contacts=[email("biz@studiokaam.in")]), only_in).passed
    assert not evaluate(creator(contacts=[email("me@gmail.com")]), only_in).passed


# -- best_email / allowed_emails -------------------------------------------


def test_best_email_respects_the_domain_filter():
    """The higher-confidence custom-domain address must not leak into a Gmail run."""
    subject = creator(
        contacts=[email("biz@studiokaam.in", conf=0.95), email("me@gmail.com", conf=0.60)]
    )
    assert subject.best(ContactKind.EMAIL).normalized == "biz@studiokaam.in"
    assert best_email(subject, GMAIL).normalized == "me@gmail.com"
    assert best_email(subject, OPEN).normalized == "biz@studiokaam.in"


def test_best_email_returns_none_when_nothing_matches():
    assert best_email(creator(contacts=[email("a@studiokaam.in")]), GMAIL) is None


def test_allowed_emails_filters_by_confidence_too():
    subject = creator(contacts=[email("a@gmail.com", conf=0.2)])
    assert allowed_emails(subject, GMAIL) == []


def test_best_email_picks_highest_among_allowed():
    subject = creator(
        contacts=[email("low@gmail.com", conf=0.55), email("high@gmail.com", conf=0.9)]
    )
    assert best_email(subject, GMAIL).normalized == "high@gmail.com"


# -- the target cap --------------------------------------------------------


def make_many(count, domain="gmail.com"):
    return [
        creator(f"c{i}", f"Creator {i}", contacts=[email(f"user{i}@{domain}")])
        for i in range(count)
    ]


def test_target_caps_the_passing_list():
    passed, _ = partition(make_many(10), OPEN, target=4)
    assert len(passed) == 4


def test_target_above_supply_returns_everything():
    passed, _ = partition(make_many(3), OPEN, target=100)
    assert len(passed) == 3


def test_no_target_returns_everything():
    passed, _ = partition(make_many(7), OPEN, target=None)
    assert len(passed) == 7


def test_target_of_zero_is_ignored_not_empty():
    passed, _ = partition(make_many(5), OPEN, target=0)
    assert len(passed) == 5


def test_target_keeps_the_best_leads_not_the_first():
    """The cap is applied after ranking, so the strongest leads survive."""
    weak = creator("weak", "Weak", contacts=[email("w@gmail.com", conf=0.55, business=False)])
    strong = creator("strong", "Strong", contacts=[email("s@gmail.com", conf=0.95), phone()])
    passed, _ = partition([weak, strong], OPEN, target=1)
    assert [c.display_name for c, _ in passed] == ["Strong"]


def test_target_does_not_shrink_the_rejected_list():
    subjects = make_many(3) + [creator("none", "No Contact")]
    passed, failed = partition(subjects, OPEN, target=1)
    assert len(passed) == 1
    assert len(failed) == 1


# -- both together, through the real CSV -----------------------------------


def test_csv_applies_both_filter_and_target(tmp_path):
    subjects = make_many(5, "gmail.com") + make_many(5, "studiokaam.in")
    # make_many reuses ids, so re-key the second batch.
    for index, subject in enumerate(subjects[5:]):
        subject.native_id = f"custom{index}"

    out = tmp_path / "leads.csv"
    written, rejected = to_csv(subjects, out, config=GMAIL, target=3)

    assert written == 3
    assert rejected == 5  # the five custom-domain creators

    with out.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert all(row["email"].endswith("@gmail.com") for row in rows)
    assert all(row["email_domain"] == "gmail.com" for row in rows)


def test_csv_email_domain_column_present(tmp_path):
    out = tmp_path / "leads.csv"
    to_csv([creator(contacts=[email("a@gmail.com")])], out, config=OPEN)
    with out.open(encoding="utf-8") as handle:
        assert csv.DictReader(handle).fieldnames.count("email_domain") == 1


def test_rejected_csv_explains_the_domain_rejection(tmp_path):
    out = tmp_path / "leads.csv"
    to_csv(
        [creator(contacts=[email("biz@studiokaam.in")])],
        out,
        config=GMAIL,
        include_rejected=True,
    )
    with (tmp_path / "leads_rejected.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert "gmail.com" in rows[0]["reject_reason"]
