"""Podcast discovery — the cleanest contact source there is.

The podcast RSS spec has ``<itunes:owner><itunes:email>`` as a *required* field
for directory submission. Every podcaster in Apple Podcasts or Spotify has
published a working contact email, in a machine-readable field, on purpose. No
scraping, no CAPTCHA, no grey area.

Two free directories are used to find feeds:

* **iTunes Search API** — no key, no auth, returns ``feedUrl``. Rate limit is
  about 20 calls/minute, which the shared :class:`Fetcher` throttle respects.
* **Podcast Index** — free key, better coverage of small and Indian shows.
  Auth is ``sha1(key + secret + unix_time)``.

Podcast directories do not publish subscriber counts, so the 50k-60k band cannot
be applied here directly. Instead each show's own links are captured; running
``enrich`` then resolves its YouTube/Instagram presence, and that is where the
audience-size filter actually bites. ``link_creator_by_name`` does the matching.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from urllib.parse import urlparse

import feedparser

from ..extract import URL_IN_TEXT_RE, extract_emails, is_plausible_email, normalize_email
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

ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
PODCASTINDEX_ROOT = "https://api.podcastindex.org/api/1.0"

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def name_key(name: str) -> str:
    """Loose key for matching a show to a channel across platforms."""
    lowered = (name or "").lower()
    lowered = re.sub(
        r"\b(the|podcast|show|official|channel|with|and|ft|feat|episode|ep)\b", " ", lowered
    )
    return _NON_ALNUM_RE.sub("", lowered)


class PodcastSource:
    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.pi_key = os.getenv("PODCASTINDEX_API_KEY", "")
        self.pi_secret = os.getenv("PODCASTINDEX_API_SECRET", "")

    # -- directories --------------------------------------------------------

    def search_itunes(self, term: str, *, country: str = "IN", limit: int = 100) -> list[dict]:
        """Shows matching ``term``. Returns raw directory entries with ``feedUrl``."""
        payload = self.fetcher.get_json(
            ITUNES_SEARCH,
            params={
                "media": "podcast",
                "entity": "podcast",
                "term": term,
                "country": country,
                "limit": min(limit, 200),
            },
        )
        results = payload.get("results", []) if isinstance(payload, dict) else []
        log.info("itunes %r -> %d shows", term, len(results))
        return results

    def _pi_headers(self) -> dict[str, str]:
        stamp = str(int(time.time()))
        digest = hashlib.sha1(
            (self.pi_key + self.pi_secret + stamp).encode("utf-8")
        ).hexdigest()
        return {
            "X-Auth-Key": self.pi_key,
            "X-Auth-Date": stamp,
            "Authorization": digest,
            "User-Agent": self.fetcher.user_agent,
        }

    def search_podcastindex(self, term: str, *, limit: int = 100) -> list[dict]:
        """Podcast Index search. Returns [] when no API key is configured.

        Podcast Index exposes ``ownerEmail`` directly in search results, so this
        path often needs no feed fetch at all.
        """
        if not (self.pi_key and self.pi_secret):
            log.debug("Podcast Index keys not set, skipping")
            return []

        try:
            response = self.fetcher._client.get(  # shares the throttled client
                f"{PODCASTINDEX_ROOT}/search/byterm",
                params={"q": term, "max": min(limit, 1000), "fulltext": 1},
                headers=self._pi_headers(),
            )
            if response.status_code >= 400:
                log.warning("Podcast Index returned %s", response.status_code)
                return []
            feeds = response.json().get("feeds", [])
        except Exception as exc:  # network, auth, or malformed JSON
            log.warning("Podcast Index search failed: %s", exc)
            return []

        log.info("podcastindex %r -> %d shows", term, len(feeds))
        return feeds

    # -- feed parsing -------------------------------------------------------

    def creator_from_feed(self, feed_url: str, *, niche: str = "") -> Creator | None:
        """Parse an RSS feed into a :class:`Creator` with its owner email."""
        result = self.fetcher.get(feed_url)
        if not result.ok:
            log.debug("feed unreachable: %s (status %s)", feed_url, result.status)
            return None

        parsed = feedparser.parse(result.text)
        channel = parsed.feed
        if not channel:
            return None

        title = (channel.get("title") or "").strip()
        if not title:
            return None

        site = (channel.get("link") or "").strip()
        summary = channel.get("summary") or channel.get("subtitle") or ""

        links = [site] if site.startswith("http") else []
        links.extend(URL_IN_TEXT_RE.findall(summary))

        creator = Creator(
            platform=Platform.PODCAST,
            native_id=feed_url,
            display_name=title,
            handle=channel.get("author") or channel.get("publisher_detail", {}).get("name", "") or "",
            profile_url=site or feed_url,
            country=(channel.get("itunes_country") or "").strip(),
            language=(channel.get("language") or "").strip(),
            niche=niche,
            description=summary[:4000],
            links=sorted({link for link in links if link.startswith("http")}),
        )

        # The owner email is the whole point of this source.
        owner_email = ""
        owner = channel.get("itunes_owner") or channel.get("owner") or {}
        if isinstance(owner, dict):
            owner_email = owner.get("email") or ""
        owner_email = owner_email or channel.get("author_detail", {}).get("email", "")
        owner_email = owner_email or channel.get("publisher_detail", {}).get("email", "")

        if owner_email:
            cleaned = normalize_email(owner_email)
            if is_plausible_email(cleaned):
                creator.add_contact(
                    ContactPoint(
                        kind=ContactKind.EMAIL,
                        value=cleaned,
                        normalized=cleaned,
                        is_business=True,
                        confidence=0.95,
                        evidence=Evidence(
                            source_url=feed_url,
                            source_type=SourceType.PODCAST_RSS_OWNER,
                            snippet="itunes:owner/itunes:email in the show's RSS feed",
                        ),
                    )
                )

        # Show notes sometimes carry a separate business address.
        for point in extract_emails(
            summary,
            source_url=feed_url,
            source_type=SourceType.PROFILE_BIO,
            respect_obfuscation=True,
        ):
            creator.add_contact(point)

        return creator

    def discover(
        self,
        terms: list[str],
        *,
        country: str = "IN",
        limit_per_term: int = 50,
        niche: str = "",
        use_podcastindex: bool = True,
    ) -> list[Creator]:
        """Search the directories, then parse each unique feed once."""
        feeds: dict[str, dict] = {}

        for term in terms:
            for entry in self.search_itunes(term, country=country, limit=limit_per_term):
                url = entry.get("feedUrl")
                if url:
                    feeds.setdefault(url, entry)
            if use_podcastindex:
                for entry in self.search_podcastindex(term, limit=limit_per_term):
                    url = entry.get("url")
                    if url:
                        feeds.setdefault(url, entry)

        creators: list[Creator] = []
        for feed_url, entry in feeds.items():
            creator = self.creator_from_feed(feed_url, niche=niche)
            if creator is None:
                # Podcast Index gives ownerEmail without needing the feed.
                creator = self._creator_from_index_entry(feed_url, entry, niche)
            if creator is None:
                continue

            genres = entry.get("genres") or entry.get("categories")
            if not creator.niche and genres:
                values = genres.values() if isinstance(genres, dict) else genres
                creator.niche = ", ".join(str(g) for g in list(values)[:3])
            creators.append(creator)

        log.info("podcast discovery -> %d creators from %d feeds", len(creators), len(feeds))
        return creators

    def _creator_from_index_entry(
        self, feed_url: str, entry: dict, niche: str
    ) -> Creator | None:
        """Fall back to directory metadata when the feed itself will not load."""
        email = entry.get("ownerEmail") or ""
        title = entry.get("title") or entry.get("collectionName") or ""
        if not title:
            return None

        site = entry.get("link") or entry.get("collectionViewUrl") or ""
        creator = Creator(
            platform=Platform.PODCAST,
            native_id=feed_url,
            display_name=title,
            handle=entry.get("author") or entry.get("artistName") or "",
            profile_url=site or feed_url,
            niche=niche,
            description=(entry.get("description") or "")[:4000],
            links=[site] if str(site).startswith("http") else [],
        )
        cleaned = normalize_email(email)
        if cleaned and is_plausible_email(cleaned):
            creator.add_contact(
                ContactPoint(
                    kind=ContactKind.EMAIL,
                    value=cleaned,
                    normalized=cleaned,
                    is_business=True,
                    confidence=0.9,
                    evidence=Evidence(
                        source_url=f"{PODCASTINDEX_ROOT}/search/byterm",
                        source_type=SourceType.PROVIDER_API,
                        snippet="ownerEmail from the Podcast Index directory",
                    ),
                )
            )
        return creator if creator.contacts else None


def link_creator_by_name(podcast: Creator, candidates: list[Creator]) -> Creator | None:
    """Match a podcast to an already-discovered channel, by name or shared domain.

    This is what lets the 50k-60k band apply to podcasters: the size filter runs
    on the matched YouTube/Instagram account, while the contact comes from RSS.
    """
    target = name_key(podcast.display_name)
    if not target:
        return None

    podcast_hosts = {
        urlparse(link).netloc.lower().removeprefix("www.")
        for link in podcast.links
        if link.startswith("http")
    }
    podcast_hosts.discard("")

    for candidate in candidates:
        if candidate.platform is Platform.PODCAST:
            continue

        key = name_key(candidate.display_name)
        if key and (key == target or (len(target) >= 6 and (target in key or key in target))):
            return candidate

        if podcast_hosts:
            candidate_hosts = {
                urlparse(link).netloc.lower().removeprefix("www.")
                for link in candidate.links
                if link.startswith("http")
            }
            if podcast_hosts & candidate_hosts:
                return candidate
    return None
