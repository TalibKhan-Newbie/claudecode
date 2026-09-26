"""Store tests against a real temporary SQLite database."""

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
from creatorcontacts.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as opened:
        yield opened


def point(kind=ContactKind.EMAIL, value="a@b.in", conf=0.8, source=SourceType.CONTACT_PAGE):
    return ContactPoint(
        kind=kind,
        value=value,
        normalized=value,
        confidence=conf,
        is_business=True,
        evidence=Evidence(
            source_url="https://site.in/contact", source_type=source, snippet="business enquiries"
        ),
    )


def creator(native="rahul", name="Rahul", followers=55_000, contacts=()):
    made = Creator(
        platform=Platform.YOUTUBE,
        native_id=native,
        display_name=name,
        handle=f"@{native}",
        profile_url=f"https://youtube.com/@{native}",
        follower_count=followers,
        country="IN",
        niche="comedy",
        links=["https://site.in", "https://instagram.com/x"],
    )
    for contact in contacts:
        made.add_contact(contact)
    return made


def test_creator_round_trips(store):
    store.upsert_creator(creator(contacts=[point()]))
    back = store.get_creator("youtube:rahul")
    assert back is not None
    assert back.display_name == "Rahul"
    assert back.follower_count == 55_000
    assert back.links == ["https://site.in", "https://instagram.com/x"]
    assert back.niche == "comedy"


def test_provenance_survives_the_round_trip(store):
    store.upsert_creator(creator(contacts=[point()]))
    contact = store.get_creator("youtube:rahul").best(ContactKind.EMAIL)
    assert contact.evidence.source_url == "https://site.in/contact"
    assert contact.evidence.source_type is SourceType.CONTACT_PAGE
    assert "business" in contact.evidence.snippet


def test_reupsert_does_not_duplicate_contacts(store):
    subject = creator(contacts=[point()])
    store.upsert_creator(subject)
    store.upsert_creator(subject)
    assert len(store.get_creator("youtube:rahul").contacts) == 1


def test_higher_confidence_wins_on_conflict(store):
    store.upsert_creator(creator(contacts=[point(conf=0.5)]))
    store.add_contact("youtube:rahul", point(conf=0.9))
    store.conn.commit()
    assert store.get_creator("youtube:rahul").best(ContactKind.EMAIL).confidence == pytest.approx(0.9)


def test_lower_confidence_does_not_downgrade(store):
    store.upsert_creator(creator(contacts=[point(conf=0.9)]))
    store.add_contact("youtube:rahul", point(conf=0.3))
    store.conn.commit()
    assert store.get_creator("youtube:rahul").best(ContactKind.EMAIL).confidence == pytest.approx(0.9)


def test_band_filter_runs_in_sql(store):
    store.upsert_creator(creator("mid", "Mid", followers=55_000, contacts=[point()]))
    store.upsert_creator(creator("big", "Big", followers=900_000, contacts=[point(value="x@y.in")]))
    store.upsert_creator(creator("tiny", "Tiny", followers=900, contacts=[point(value="z@y.in")]))

    names = [c.display_name for c in store.iter_creators(min_followers=50_000, max_followers=60_000)]
    assert names == ["Mid"]


def test_platform_filter(store):
    store.upsert_creator(creator())
    podcast = Creator(platform=Platform.PODCAST, native_id="feed", display_name="Show")
    store.upsert_creator(podcast)
    assert [c.display_name for c in store.iter_creators(platform=Platform.PODCAST)] == ["Show"]


def test_suppression_deletes_and_blocks_readding(store):
    store.upsert_creator(creator(contacts=[point()]))
    store.suppress("a@b.in", reason="asked to be removed")

    assert store.is_suppressed("a@b.in")
    assert store.get_creator("youtube:rahul").contacts == []

    # A later scrape must not resurrect it.
    store.upsert_creator(creator(contacts=[point()]))
    assert store.get_creator("youtube:rahul").contacts == []


def test_suppression_is_case_insensitive(store):
    store.suppress("Someone@Example.IN")
    assert store.is_suppressed("someone@example.in")
    assert store.is_suppressed("SOMEONE@EXAMPLE.IN")


def test_unsuppress(store):
    store.suppress("a@b.in")
    store.unsuppress("a@b.in")
    assert not store.is_suppressed("a@b.in")
    store.upsert_creator(creator(contacts=[point()]))
    assert len(store.get_creator("youtube:rahul").contacts) == 1


def test_needs_enrichment_flag(store):
    store.upsert_creator(creator(contacts=[point()]))
    assert [c.creator_id for c in store.iter_creators(needs_enrichment=True)] == ["youtube:rahul"]
    store.mark_enriched("youtube:rahul")
    assert list(store.iter_creators(needs_enrichment=True)) == []
    assert store.get_creator("youtube:rahul").enriched_at is not None


def test_http_cache_round_trip(store):
    store.cache_put("https://x.in", 200, "<html>hi</html>", "text/html")
    assert store.cache_get("https://x.in") == (200, "<html>hi</html>", "text/html")
    assert store.cache_get("https://missing.in") is None
    assert store.clear_cache() == 1
    assert store.cache_get("https://x.in") is None


def test_cache_body_is_capped(store):
    from creatorcontacts.store import MAX_CACHE_CHARS

    store.cache_put("https://big.in", 200, "x" * (MAX_CACHE_CHARS + 5_000), "text/html")
    _, body, _ = store.cache_get("https://big.in")
    assert len(body) == MAX_CACHE_CHARS


def test_set_links(store):
    store.upsert_creator(creator())
    store.set_links("youtube:rahul", ["https://new.in"])
    assert store.get_creator("youtube:rahul").links == ["https://new.in"]


def test_stats(store):
    store.upsert_creator(creator(contacts=[point(), point(ContactKind.PHONE, "+919876543210")]))
    data = store.stats()
    assert data["creators"] == 1
    assert data["contacts"] == 2
    assert data["creators_by_platform"] == {"youtube": 1}
    assert data["contacts_by_kind"] == {"email": 1, "phone": 1}
    assert data["with_email"] == 1
    assert data["business_emails"] == 1
    assert data["unenriched"] == 1


def test_missing_creator_returns_none(store):
    assert store.get_creator("youtube:nobody") is None


def test_contacts_cascade_on_creator_delete(store):
    store.upsert_creator(creator(contacts=[point()]))
    store.conn.execute("DELETE FROM creators WHERE creator_id = ?", ("youtube:rahul",))
    store.conn.commit()
    assert store.contacts_for("youtube:rahul") == []
