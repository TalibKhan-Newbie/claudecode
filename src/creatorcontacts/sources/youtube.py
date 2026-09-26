"""YouTube discovery via the official Data API v3.

Finding creators in a narrow subscriber band is a funnel problem, not a scraping
problem. ``search.list`` costs 100 quota units and will not filter by subscriber
count, so the cheap path is: search *videos* (which surfaces small channels that
channel-search buries), collect their channel IDs, then hydrate them 50 at a
time with ``channels.list`` at 1 unit per call. One 100-unit search yields up to
50 channels checked for 1 more unit.

Two things to know about the numbers:

* YouTube rounds public subscriber counts to three significant figures above
  1,000 — a channel reporting 54,300 could be anywhere in 54,250-54,349. For a
  50k-60k band that rounding is irrelevant, but do not treat the count as exact.
* A channel can hide its count. Those arrive with ``follower_count_hidden`` set
  and are excluded from band filtering rather than silently dropped.

The business email on a channel's About page sits behind a CAPTCHA and is not in
the API. We do not touch it. What we use instead is the channel description
(creators very often paste the same address there in plain text) and the
channel's own published links, which ``links.py`` then follows.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from ..extract import URL_IN_TEXT_RE, extract_emails
from ..models import Creator, Platform, SourceType
from ..net import Fetcher

log = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/youtube/v3"

#: Quota cost per endpoint, for the budget tracker.
QUOTA_COSTS = {"search": 100, "channels": 1, "videos": 1, "playlistItems": 1}


class QuotaExceeded(RuntimeError):
    """The configured daily quota budget is spent."""


@dataclass
class QuotaBudget:
    """Tracks Data API units so a run stops before Google cuts it off.

    The default daily allowance for a new project is 10,000 units.
    """

    limit: int = 10_000
    spent: int = 0
    _log: dict[str, int] = field(default_factory=dict)

    def charge(self, endpoint: str, calls: int = 1) -> None:
        cost = QUOTA_COSTS.get(endpoint, 1) * calls
        if self.spent + cost > self.limit:
            raise QuotaExceeded(
                f"{endpoint} needs {cost} units, {self.remaining} left of {self.limit}"
            )
        self.spent += cost
        self._log[endpoint] = self._log.get(endpoint, 0) + cost

    @property
    def remaining(self) -> int:
        return max(self.limit - self.spent, 0)

    def summary(self) -> str:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(self._log.items()))
        return f"{self.spent}/{self.limit} units ({detail or 'none'})"


class YouTubeSource:
    def __init__(
        self,
        fetcher: Fetcher,
        api_key: str | None = None,
        budget: QuotaBudget | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("YOUTUBE_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "YOUTUBE_API_KEY is not set. Create a key at "
                "https://console.cloud.google.com/apis/credentials and enable "
                "the YouTube Data API v3."
            )
        self.fetcher = fetcher
        self.budget = budget or QuotaBudget()

    def _call(self, endpoint: str, params: dict) -> dict:
        self.budget.charge(endpoint)
        payload = self.fetcher.get_json(
            f"{API_ROOT}/{endpoint}", params={**params, "key": self.api_key}
        )
        if not isinstance(payload, dict):
            return {}
        if "error" in payload:
            message = payload["error"].get("message", "unknown error")
            reasons = {e.get("reason") for e in payload["error"].get("errors", [])}
            if "quotaExceeded" in reasons:
                raise QuotaExceeded(f"YouTube says quota is exhausted: {message}")
            log.error("YouTube API error on %s: %s", endpoint, message)
            return {}
        return payload

    # -- discovery ----------------------------------------------------------

    def search_channel_ids(
        self,
        query: str,
        *,
        region: str = "IN",
        language: str | None = "hi",
        shorts_only: bool = False,
        published_after: str | None = None,
        pages: int = 2,
        order: str = "relevance",
    ) -> list[str]:
        """Channel IDs behind videos matching ``query``.

        Searching videos rather than channels is deliberate: video search reaches
        small channels that channel-search ranks below the big accounts, which is
        exactly where a 50k-60k creator lives.
        """
        ids: dict[str, None] = {}
        page_token: str | None = None

        for _ in range(max(pages, 1)):
            params = {
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": 50,
                "order": order,
                "regionCode": region,
            }
            if language:
                params["relevanceLanguage"] = language
            if shorts_only:
                params["videoDuration"] = "short"
            if published_after:
                params["publishedAfter"] = published_after
            if page_token:
                params["pageToken"] = page_token

            payload = self._call("search", params)
            for item in payload.get("items", []):
                channel_id = item.get("snippet", {}).get("channelId")
                if channel_id:
                    ids.setdefault(channel_id, None)

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        log.info("search %r -> %d channel ids (%s)", query, len(ids), self.budget.summary())
        return list(ids)

    def hydrate_channels(self, channel_ids: list[str]) -> list[Creator]:
        """Full channel records for up to 50 IDs per call, at 1 unit each."""
        creators: list[Creator] = []

        for start in range(0, len(channel_ids), 50):
            chunk = channel_ids[start : start + 50]
            payload = self._call(
                "channels",
                {
                    "part": "snippet,statistics,brandingSettings,topicDetails,status",
                    "id": ",".join(chunk),
                    "maxResults": 50,
                },
            )
            for item in payload.get("items", []):
                creator = self._to_creator(item)
                if creator:
                    creators.append(creator)
        return creators

    def _to_creator(self, item: dict) -> Creator | None:
        channel_id = item.get("id")
        if not channel_id:
            return None

        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        branding = item.get("brandingSettings", {}).get("channel", {})

        hidden = str(stats.get("hiddenSubscriberCount", "false")).lower() == "true"
        raw_count = stats.get("subscriberCount")
        follower_count = None
        if not hidden and raw_count is not None:
            try:
                follower_count = int(raw_count)
            except (TypeError, ValueError):
                follower_count = None

        description = snippet.get("description") or branding.get("description") or ""
        handle = snippet.get("customUrl", "") or ""
        profile_url = (
            f"https://www.youtube.com/{handle}"
            if handle.startswith("@")
            else f"https://www.youtube.com/channel/{channel_id}"
        )

        creator = Creator(
            platform=Platform.YOUTUBE,
            native_id=channel_id,
            display_name=snippet.get("title", ""),
            handle=handle,
            profile_url=profile_url,
            follower_count=follower_count,
            follower_count_hidden=hidden,
            country=snippet.get("country", "") or branding.get("country", "") or "",
            language=(
                snippet.get("defaultLanguage")
                or branding.get("defaultLanguage")
                or ""
            ),
            description=description,
            links=sorted(set(URL_IN_TEXT_RE.findall(description))),
        )

        # The description is prose the creator wrote, so emails yes, phones no.
        for point in extract_emails(
            description,
            source_url=profile_url,
            source_type=SourceType.CHANNEL_DESCRIPTION,
            respect_obfuscation=True,
        ):
            creator.add_contact(point)

        return creator

    def discover(
        self,
        queries: list[str],
        *,
        min_followers: int = 50_000,
        max_followers: int = 60_000,
        region: str = "IN",
        language: str | None = "hi",
        shorts_only: bool = False,
        published_after: str | None = None,
        pages: int = 2,
        niche: str = "",
        include_hidden_counts: bool = False,
    ) -> list[Creator]:
        """Search, hydrate, and keep only creators inside the subscriber band."""
        seen_ids: dict[str, None] = {}
        for query in queries:
            try:
                for channel_id in self.search_channel_ids(
                    query,
                    region=region,
                    language=language,
                    shorts_only=shorts_only,
                    published_after=published_after,
                    pages=pages,
                ):
                    seen_ids.setdefault(channel_id, None)
            except QuotaExceeded as exc:
                log.warning("stopping discovery early: %s", exc)
                break

        if not seen_ids:
            return []

        try:
            hydrated = self.hydrate_channels(list(seen_ids))
        except QuotaExceeded as exc:
            log.warning("stopping hydration early: %s", exc)
            return []

        kept: list[Creator] = []
        for creator in hydrated:
            if creator.follower_count_hidden:
                if include_hidden_counts:
                    creator.niche = niche
                    kept.append(creator)
                continue
            count = creator.follower_count
            if count is None or not (min_followers <= count <= max_followers):
                continue
            creator.niche = niche
            kept.append(creator)

        log.info(
            "kept %d/%d channels in %s-%s band (%s)",
            len(kept),
            len(hydrated),
            f"{min_followers:,}",
            f"{max_followers:,}",
            self.budget.summary(),
        )
        return kept
