"""Follow a creator's own published links to find their contact page.

The path is always: profile -> link hub (Linktree and friends) -> real website ->
``/contact``. Every hop is a link the creator chose to publish, and every fetch
goes through :class:`Fetcher`, so robots.txt and rate limits apply throughout.

Hosts we never crawl are listed in :data:`SKIP_HOSTS`. Two kinds are in there:
platforms whose terms forbid it (Instagram, Facebook, LinkedIn), and people-search
/ data-broker sites. The second group is the important one — those sites are how a
tool like this would turn into a doxxing tool, so they are blocked at the network
layer rather than left to discipline.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..extract import (
    CONTACT_PAGE_PATHS,
    extract_from_html,
    extract_outbound_links,
    is_link_hub,
)
from ..models import ContactKind, ContactPoint, Creator, Evidence, SourceType
from ..net import FetchBlocked, Fetcher, candidate_contact_urls

log = logging.getLogger(__name__)

#: Never fetched. Platforms whose ToS forbid scraping, plus people-search sites.
SKIP_HOSTS = frozenset(
    {
        # Platforms that forbid automated access in their terms.
        "instagram.com",
        "facebook.com",
        "fb.com",
        "linkedin.com",
        "x.com",
        "twitter.com",
        "tiktok.com",
        "threads.net",
        "snapchat.com",
        "pinterest.com",
        "reddit.com",
        "quora.com",
        # People-search and data brokers. Out of scope by design: these sell
        # residential addresses and personal numbers, which is not business
        # contact info. See docs/SCOPE.md.
        "truecaller.com",
        "whitepages.com",
        "spokeo.com",
        "beenverified.com",
        "peoplefinders.com",
        "intelius.com",
        "fastpeoplesearch.com",
        "truepeoplesearch.com",
        "thatsthem.com",
        "radaris.com",
        "usphonebook.com",
        "anywho.com",
        "zabasearch.com",
        "instantcheckmate.com",
        "socialcatfish.com",
        "clearbit.com",
        "rocketreach.co",
        "signalhire.com",
        "lusha.com",
        "snov.io",
        "hunter.io",
        # Noise.
        "youtube.com",
        "youtu.be",
        "google.com",
        "goo.gl",
        "bit.ly",
        "amazon.in",
        "amazon.com",
        "spotify.com",
        "apple.com",
        "paypal.com",
        "patreon.com",
        "wa.me",
        "t.me",
        "discord.gg",
        "discord.com",
    }
)

#: Social hosts we record as a SOCIAL contact without fetching.
SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "linkedin.com": "linkedin",
    "tiktok.com": "tiktok",
    "threads.net": "threads",
    "facebook.com": "facebook",
}


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def should_skip(url: str) -> bool:
    """True when ``url`` must not be fetched.

    Fails closed: anything that is not a parseable http(s) URL is skipped, so a
    ``ftp://`` or ``javascript:`` link in a creator's bio never reaches the
    fetcher. Matching is host-exact or on a dot-boundary, so ``nottruecaller.in``
    does not match ``truecaller.com``.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True
    host = parsed.netloc.lower().removeprefix("www.")
    if not host:
        return True
    return any(host == blocked or host.endswith("." + blocked) for blocked in SKIP_HOSTS)


class LinkEnricher:
    """Walks a creator's links and adds whatever contact info they publish."""

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        respect_obfuscation: bool = True,
        max_pages_per_creator: int = 8,
        region: str = "IN",
    ) -> None:
        self.fetcher = fetcher
        self.respect_obfuscation = respect_obfuscation
        self.max_pages = max_pages_per_creator
        self.region = region

    def _fetch(self, url: str) -> str:
        try:
            result = self.fetcher.get(url)
        except FetchBlocked as exc:
            log.debug("skipped by robots.txt: %s", exc)
            return ""
        if not result.ok:
            return ""
        return result.text

    def expand_hubs(self, links: list[str]) -> list[str]:
        """Replace each Linktree-style URL with the links it points at."""
        expanded: dict[str, None] = {}

        for link in links:
            if not link.startswith(("http://", "https://")):
                continue
            if not is_link_hub(link):
                expanded.setdefault(link, None)
                continue

            html = self._fetch(link)
            if not html:
                continue
            # A hub page is itself worth scanning; creators put emails on them.
            for outbound in extract_outbound_links(html, base_url=link, limit=25):
                if not should_skip(outbound):
                    expanded.setdefault(outbound, None)
            expanded.setdefault(link, None)

        return list(expanded)

    def enrich(self, creator: Creator) -> int:
        """Crawl ``creator.links`` and attach found contacts. Returns count added."""
        added = 0
        budget = self.max_pages
        visited: set[str] = set()

        candidates = self.expand_hubs(creator.links)

        # Record social handles without fetching those hosts.
        for link in candidates:
            host = _host(link)
            for social_host, label in SOCIAL_HOSTS.items():
                if host == social_host or host.endswith("." + social_host):
                    if creator.add_contact(
                        ContactPoint(
                            kind=ContactKind.SOCIAL,
                            value=link,
                            normalized=f"{label}:{link.rstrip('/').rsplit('/', 1)[-1].lower()}",
                            confidence=0.6,
                            evidence=Evidence(
                                source_url=creator.profile_url,
                                source_type=SourceType.PROFILE_BIO,
                                snippet=f"{label} link published on the creator's profile",
                            ),
                        )
                    ):
                        added += 1
                    break

        crawlable = [link for link in candidates if not should_skip(link)]

        for link in crawlable:
            if budget <= 0:
                break

            for url in candidate_contact_urls(link, CONTACT_PAGE_PATHS):
                if budget <= 0:
                    break
                if url in visited:
                    continue
                visited.add(url)

                html = self._fetch(url)
                budget -= 1
                if not html:
                    continue

                path = urlparse(url).path.lower().rstrip("/")
                is_contact_page = any(
                    path == candidate or path.startswith(candidate + "/")
                    for candidate in CONTACT_PAGE_PATHS
                )
                source_type = (
                    SourceType.CONTACT_PAGE
                    if is_contact_page
                    else (SourceType.LINK_HUB if is_link_hub(url) else SourceType.WEBSITE_BODY)
                )

                for point in extract_from_html(
                    html,
                    source_url=url,
                    source_type=source_type,
                    region=self.region,
                    respect_obfuscation=self.respect_obfuscation,
                ):
                    if creator.add_contact(point):
                        added += 1

        if added:
            log.info("%s -> +%d contacts", creator.display_name or creator.creator_id, added)
        return added
