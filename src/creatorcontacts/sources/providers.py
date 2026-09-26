"""Licensed influencer-data providers, for Instagram and TikTok.

Read this before you try to scrape Instagram.

Instagram has no public search API. The Graph API only reaches accounts that have
authorised *your* app, and ``business_discovery`` returns follower counts but not
emails, and only for business/creator accounts. There is no compliant way to
enumerate "all Indian comedy creators with 50-60k followers" from Meta's own API.
Scraping the web UI violates Meta's terms, and the ``hiQ v. LinkedIn`` line of
cases protects *public* scraping from criminal CFAA liability — it does not void
the contract you accepted, and Meta has sued scrapers on exactly that basis.

So for Instagram and TikTok discovery at scale, the working answer is a licensed
vendor. They hold the platform agreements and pass through a creator's public
business email in their API:

===============  ============================================  ==================
Vendor           Notes                                         Rough entry price
===============  ============================================  ==================
Modash           Good India coverage, follower-range filters,   from ~$200/mo
                 returns public business email
Phyllo/InsightIQ Creator-authorised data, strong compliance     usage-based
                 story, official platform partner
HypeAuditor      Fraud/authenticity scoring alongside contacts  from ~$400/mo
Upfluence        CRM-shaped, bulk export                        quote only
===============  ============================================  ==================

Both adapters below are real HTTP clients against the documented endpoints, gated
on an API key. Without a key they return ``[]`` and log why — nothing here
silently falls back to scraping.

YouTube and podcasts need none of this: the Data API and the RSS spec cover them
for free, which is why those two sources carry the project.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

from ..extract import is_plausible_email, normalize_email
from ..models import (
    ContactKind,
    ContactPoint,
    Creator,
    Evidence,
    Platform,
    SourceType,
)
from ..net import Fetcher

log = logging.getLogger(__name__)


class InfluencerProvider(Protocol):
    """Common shape so the CLI can treat every vendor identically."""

    name: str

    def available(self) -> bool: ...

    def search(
        self,
        *,
        platform: Platform,
        min_followers: int,
        max_followers: int,
        niche: str = "",
        country: str = "",
        limit: int = 100,
    ) -> list[Creator]: ...


def _creator_with_email(
    *,
    platform: Platform,
    native_id: str,
    display_name: str,
    handle: str,
    profile_url: str,
    followers: int | None,
    country: str,
    niche: str,
    email: str,
    provider_name: str,
    provider_url: str,
) -> Creator:
    creator = Creator(
        platform=platform,
        native_id=native_id,
        display_name=display_name,
        handle=handle,
        profile_url=profile_url,
        follower_count=followers,
        country=country,
        niche=niche,
    )
    cleaned = normalize_email(email or "")
    if cleaned and is_plausible_email(cleaned):
        creator.add_contact(
            ContactPoint(
                kind=ContactKind.EMAIL,
                value=cleaned,
                normalized=cleaned,
                is_business=True,
                confidence=0.85,
                evidence=Evidence(
                    source_url=provider_url,
                    source_type=SourceType.PROVIDER_API,
                    snippet=f"public business email supplied by {provider_name}",
                ),
            )
        )
    return creator


class ModashProvider:
    """https://docs.modash.io — Discovery API."""

    name = "modash"
    ROOT = "https://api.modash.io/v1"

    def __init__(self, fetcher: Fetcher, api_key: str | None = None) -> None:
        self.fetcher = fetcher
        self.api_key = api_key or os.getenv("MODASH_API_KEY", "")

    def available(self) -> bool:
        return bool(self.api_key)

    def search(
        self,
        *,
        platform: Platform,
        min_followers: int,
        max_followers: int,
        niche: str = "",
        country: str = "",
        limit: int = 100,
    ) -> list[Creator]:
        if not self.available():
            log.info("MODASH_API_KEY not set — skipping Modash")
            return []

        platform_slug = {
            Platform.INSTAGRAM: "instagram",
            Platform.TIKTOK: "tiktok",
            Platform.YOUTUBE: "youtube",
        }.get(platform)
        if not platform_slug:
            return []

        body: dict = {
            "page": 0,
            "limit": min(limit, 100),
            "sort": {"field": "followers", "direction": "asc"},
            "filter": {
                "influencer": {
                    "followers": {"min": min_followers, "max": max_followers},
                    "hasContactDetails": [{"contactType": "email", "filterAction": "must"}],
                }
            },
        }
        if country:
            body["filter"]["influencer"]["location"] = [country]
        if niche:
            body["filter"]["influencer"]["keywords"] = niche

        try:
            response = self.fetcher._client.post(
                f"{self.ROOT}/{platform_slug}/search",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30.0,
            )
            if response.status_code >= 400:
                log.warning("Modash returned %s: %s", response.status_code, response.text[:300])
                return []
            payload = response.json()
        except Exception as exc:
            log.warning("Modash search failed: %s", exc)
            return []

        creators: list[Creator] = []
        for entry in payload.get("lookalikes", []) or payload.get("directs", []) or []:
            profile = entry.get("profile", entry)
            email = ""
            for contact in profile.get("contacts", []) or []:
                if contact.get("type") == "email":
                    email = contact.get("value", "")
                    break
            creators.append(
                _creator_with_email(
                    platform=platform,
                    native_id=str(profile.get("userId") or profile.get("username") or ""),
                    display_name=profile.get("fullname", "") or profile.get("username", ""),
                    handle=profile.get("username", ""),
                    profile_url=profile.get("url", ""),
                    followers=profile.get("followers"),
                    country=country,
                    niche=niche,
                    email=email,
                    provider_name="Modash",
                    provider_url=f"{self.ROOT}/{platform_slug}/search",
                )
            )
        log.info("Modash -> %d creators", len(creators))
        return [c for c in creators if c.native_id]


class PhylloProvider:
    """https://docs.insightiq.ai — Creator Search (formerly Phyllo)."""

    name = "phyllo"
    ROOT = "https://api.insightiq.ai/v1"

    PLATFORM_IDS = {
        Platform.INSTAGRAM: "9bb8913b-ddd9-430b-a66a-d74d846e6c66",
        Platform.YOUTUBE: "14d9ddf5-51c6-415e-bde6-f8ed36ad7054",
        Platform.TIKTOK: "de55aeec-0dc8-4119-bf90-16b3d1f0c987",
    }

    def __init__(self, fetcher: Fetcher, client_id: str | None = None, secret: str | None = None) -> None:
        self.fetcher = fetcher
        self.client_id = client_id or os.getenv("INSIGHTIQ_CLIENT_ID", "")
        self.secret = secret or os.getenv("INSIGHTIQ_SECRET", "")

    def available(self) -> bool:
        return bool(self.client_id and self.secret)

    def search(
        self,
        *,
        platform: Platform,
        min_followers: int,
        max_followers: int,
        niche: str = "",
        country: str = "",
        limit: int = 100,
    ) -> list[Creator]:
        if not self.available():
            log.info("INSIGHTIQ_CLIENT_ID/SECRET not set — skipping Phyllo")
            return []

        platform_id = self.PLATFORM_IDS.get(platform)
        if not platform_id:
            return []

        body: dict = {
            "work_platform_id": platform_id,
            "follower_count": {"min": min_followers, "max": max_followers},
            "has_contact_details": True,
            "limit": min(limit, 100),
            "offset": 0,
        }
        if country:
            body["creator_locations"] = [country]
        if niche:
            body["description_keywords"] = niche

        try:
            response = self.fetcher._client.post(
                f"{self.ROOT}/social/creators/profiles/search",
                json=body,
                auth=(self.client_id, self.secret),
                timeout=30.0,
            )
            if response.status_code >= 400:
                log.warning("Phyllo returned %s: %s", response.status_code, response.text[:300])
                return []
            payload = response.json()
        except Exception as exc:
            log.warning("Phyllo search failed: %s", exc)
            return []

        creators: list[Creator] = []
        for profile in payload.get("data", []) or []:
            email = ""
            for detail in profile.get("contact_details", []) or []:
                if detail.get("type") in ("email", "EMAIL"):
                    email = detail.get("value", "")
                    break
            creators.append(
                _creator_with_email(
                    platform=platform,
                    native_id=str(profile.get("platform_username") or profile.get("external_id") or ""),
                    display_name=profile.get("full_name", "") or profile.get("platform_username", ""),
                    handle=profile.get("platform_username", ""),
                    profile_url=profile.get("url", ""),
                    followers=profile.get("follower_count"),
                    country=country,
                    niche=niche,
                    email=email,
                    provider_name="InsightIQ/Phyllo",
                    provider_url=f"{self.ROOT}/social/creators/profiles/search",
                )
            )
        log.info("Phyllo -> %d creators", len(creators))
        return [c for c in creators if c.native_id]


def all_providers(fetcher: Fetcher) -> list[InfluencerProvider]:
    return [ModashProvider(fetcher), PhylloProvider(fetcher)]


def available_providers(fetcher: Fetcher) -> list[InfluencerProvider]:
    return [p for p in all_providers(fetcher) if p.available()]
