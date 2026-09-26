"""Export tests — the CSV is the deliverable, so its columns are asserted."""

from __future__ import annotations

import csv

from creatorcontacts.export import COLUMNS, to_csv
from creatorcontacts.models import (
    ContactKind,
    ContactPoint,
    Creator,
    Evidence,
    Platform,
    SourceType,
)
from creatorcontacts.score import GateConfig

GATE = GateConfig(min_fields=2, require_reachable=True)


def point(kind, value, source=SourceType.CONTACT_PAGE, url="https://site.in/contact", conf=0.85):
    return ContactPoint(
        kind=kind,
        value=value,
        normalized=value,
        confidence=conf,
        is_business=True,
        evidence=Evidence(source_url=url, source_type=source),
    )


def creator(native="rahul", name="Rahul Comedy", followers=55_000, contacts=(), links=None):
    made = Creator(
        platform=Platform.YOUTUBE,
        native_id=native,
        display_name=name,
        handle=f"@{native}",
        profile_url=f"https://youtube.com/@{native}",
        follower_count=followers,
        country="IN",
        language="hi",
        niche="comedy",
        links=links if links is not None else ["https://instagram.com/x", "https://studiokaam.in"],
    )
    for contact in contacts:
        made.add_contact(contact)
    return made


def read(path):
    with open(path, encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_only_passing_rows_are_written(tmp_path):
    out = tmp_path / "leads.csv"
    written, rejected = to_csv(
        [
            creator("pass", contacts=[point(ContactKind.EMAIL, "a@b.in")]),
            creator("fail", "No Contact"),
        ],
        out,
        config=GATE,
    )
    assert (written, rejected) == (1, 1)
    rows = read(out)
    assert len(rows) == 1
    assert rows[0]["name"] == "Rahul Comedy"


def test_all_columns_present_in_header(tmp_path):
    out = tmp_path / "leads.csv"
    to_csv([creator(contacts=[point(ContactKind.EMAIL, "a@b.in")])], out, config=GATE)
    with open(out, encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == COLUMNS


def test_contact_columns_and_provenance_are_filled(tmp_path):
    out = tmp_path / "leads.csv"
    to_csv(
        [
            creator(
                contacts=[
                    point(ContactKind.EMAIL, "biz@studiokaam.in"),
                    point(ContactKind.PHONE, "+919876543210", SourceType.TEL_LINK),
                    point(ContactKind.ADDRESS, "21 Link Road, Mumbai 400053", SourceType.SCHEMA_ORG),
                ]
            )
        ],
        out,
        config=GATE,
    )
    row = read(out)[0]
    assert row["email"] == "biz@studiokaam.in"
    assert row["email_is_business"] == "yes"
    assert row["email_source_url"] == "https://site.in/contact"
    assert row["email_source_type"] == "contact_page"
    assert row["phone"] == "+919876543210"
    assert row["phone_source_url"] == "https://site.in/contact"
    assert "Link Road" in row["address"]
    assert row["address_source_url"] == "https://site.in/contact"


def test_website_column_skips_social_hosts(tmp_path):
    out = tmp_path / "leads.csv"
    to_csv([creator(contacts=[point(ContactKind.EMAIL, "a@b.in")])], out, config=GATE)
    assert read(out)[0]["website"] == "https://studiokaam.in"


def test_website_column_empty_when_only_socials(tmp_path):
    out = tmp_path / "leads.csv"
    to_csv(
        [
            creator(
                contacts=[point(ContactKind.EMAIL, "a@b.in")],
                links=["https://instagram.com/x", "https://youtube.com/@x"],
            )
        ],
        out,
        config=GATE,
    )
    assert read(out)[0]["website"] == ""


def test_highest_confidence_email_is_the_exported_one(tmp_path):
    out = tmp_path / "leads.csv"
    subject = creator(
        contacts=[
            point(ContactKind.EMAIL, "weak@b.in", conf=0.55),
            point(ContactKind.EMAIL, "strong@b.in", conf=0.95),
        ]
    )
    to_csv([subject], out, config=GATE)
    assert read(out)[0]["email"] == "strong@b.in"


def test_rejected_file_carries_reasons(tmp_path):
    out = tmp_path / "leads.csv"
    to_csv([creator("fail", "No Contact")], out, config=GATE, include_rejected=True)
    rejected = read(tmp_path / "leads_rejected.csv")
    assert len(rejected) == 1
    assert "email or phone" in rejected[0]["reject_reason"]


def test_no_rejected_file_unless_requested(tmp_path):
    out = tmp_path / "leads.csv"
    to_csv([creator("fail", "No Contact")], out, config=GATE)
    assert not (tmp_path / "leads_rejected.csv").exists()


def test_hidden_follower_count_marked(tmp_path):
    out = tmp_path / "leads.csv"
    subject = creator(followers=None, contacts=[point(ContactKind.EMAIL, "a@b.in")])
    subject.follower_count_hidden = True
    to_csv([subject], out, config=GATE)
    row = read(out)[0]
    assert row["followers"] == ""
    assert row["followers_hidden"] == "yes"


def test_socials_joined_into_one_column(tmp_path):
    out = tmp_path / "leads.csv"
    subject = creator(
        contacts=[
            point(ContactKind.EMAIL, "a@b.in"),
            point(ContactKind.SOCIAL, "https://instagram.com/x", SourceType.PROFILE_BIO),
            point(ContactKind.SOCIAL, "https://x.com/y", SourceType.PROFILE_BIO),
        ]
    )
    to_csv([subject], out, config=GATE)
    assert read(out)[0]["socials"].count("|") == 1


def test_empty_input_writes_header_only(tmp_path):
    out = tmp_path / "leads.csv"
    assert to_csv([], out, config=GATE) == (0, 0)
    assert read(out) == []


def test_output_directory_is_created(tmp_path):
    out = tmp_path / "nested" / "dir" / "leads.csv"
    to_csv([creator(contacts=[point(ContactKind.EMAIL, "a@b.in")])], out, config=GATE)
    assert out.exists()
