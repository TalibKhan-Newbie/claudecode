"""Core data model.

Every contact value carries its own :class:`Evidence` — the URL it came from and
the surrounding text. That provenance is not decoration: it is what lets you
answer "where did you get my email?" and what the gating in ``score.py`` reads
to decide whether a field is business contact info or a lucky regex hit.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Platform(str, enum.Enum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    PODCAST = "podcast"
    WEBSITE = "website"


class ContactKind(str, enum.Enum):
    NAME = "name"
    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"
    SOCIAL = "social"


class SourceType(str, enum.Enum):
    """Where a value was read from, ordered loosely by how much we trust it."""

    # Creator published it in a field whose entire purpose is contact.
    PODCAST_RSS_OWNER = "podcast_rss_owner"
    MAILTO_LINK = "mailto_link"
    TEL_LINK = "tel_link"
    SCHEMA_ORG = "schema_org"
    CONTACT_PAGE = "contact_page"
    # Creator published it as prose, so it needs more interpretation.
    CHANNEL_DESCRIPTION = "channel_description"
    PROFILE_BIO = "profile_bio"
    WEBSITE_BODY = "website_body"
    LINK_HUB = "link_hub"
    # Came from a licensed influencer-data vendor.
    PROVIDER_API = "provider_api"
    PLATFORM_API = "platform_api"


#: Source types we accept for a postal address or a phone number. Both fields
#: only fill from places a business publishes deliberately — a labelled contact
#: page, structured business markup, or a ``tel:`` link. Free prose is never
#: mined for them, which is what keeps this an outreach tool and not a
#: people-finder. See ``docs/SCOPE.md``.
BUSINESS_ONLY_SOURCES = frozenset(
    {
        SourceType.SCHEMA_ORG,
        SourceType.CONTACT_PAGE,
        SourceType.TEL_LINK,
        SourceType.PODCAST_RSS_OWNER,
        SourceType.PROVIDER_API,
    }
)

#: Words near a value that mark it as published for business contact.
BUSINESS_MARKERS = (
    "business",
    "business enquiries",
    "business inquiries",
    "enquiries",
    "inquiries",
    "collab",
    "collaboration",
    "brand",
    "sponsor",
    "sponsorship",
    "promotion",
    "management",
    "manager",
    "agency",
    "booking",
    "bookings",
    "press",
    "media",
    "pr ",
    "work with me",
    "for work",
    "partnership",
    "advertising",
)


@dataclass(frozen=True)
class Evidence:
    """Audit trail for one extracted value."""

    source_url: str
    source_type: SourceType
    snippet: str = ""
    fetched_at: datetime = field(default_factory=_now)

    def redacted_snippet(self, value: str, keep: int = 2) -> str:
        """The snippet with ``value`` partly masked, for logs and dry runs."""
        if not value or value not in self.snippet:
            return self.snippet
        masked = value[:keep] + "*" * max(len(value) - keep, 0)
        return self.snippet.replace(value, masked)


@dataclass
class ContactPoint:
    kind: ContactKind
    value: str
    evidence: Evidence
    normalized: str = ""
    confidence: float = 0.0
    is_business: bool = False
    #: Set when the creator had obfuscated the value (``name [at] site.com``).
    was_obfuscated: bool = False

    def __post_init__(self) -> None:
        if not self.normalized:
            self.normalized = self.value.strip()

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return (self.kind.value, self.normalized.lower())


@dataclass
class Creator:
    """One creator, on one platform."""

    platform: Platform
    native_id: str
    display_name: str = ""
    handle: str = ""
    profile_url: str = ""
    follower_count: int | None = None
    #: True when the platform hides the real count (YouTube lets channels do this).
    follower_count_hidden: bool = False
    country: str = ""
    language: str = ""
    niche: str = ""
    description: str = ""
    #: Outbound links the creator published on their profile.
    links: list[str] = field(default_factory=list)
    contacts: list[ContactPoint] = field(default_factory=list)
    discovered_at: datetime = field(default_factory=_now)
    enriched_at: datetime | None = None

    @property
    def creator_id(self) -> str:
        return f"{self.platform.value}:{self.native_id}"

    def add_contact(self, point: ContactPoint) -> bool:
        """Add ``point`` unless an equal-or-better duplicate is already held."""
        for existing in self.contacts:
            if existing.dedupe_key != point.dedupe_key:
                continue
            if point.confidence > existing.confidence:
                self.contacts.remove(existing)
                self.contacts.append(point)
                return True
            # Keep the better evidence but remember it is corroborated.
            existing.is_business = existing.is_business or point.is_business
            return False
        self.contacts.append(point)
        return True

    def contacts_of(self, kind: ContactKind) -> list[ContactPoint]:
        return [c for c in self.contacts if c.kind is kind]

    def best(self, kind: ContactKind) -> ContactPoint | None:
        found = self.contacts_of(kind)
        return max(found, key=lambda c: c.confidence) if found else None
