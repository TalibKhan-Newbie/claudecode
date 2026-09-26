#!/usr/bin/env python3
"""Standalone podcast contact finder — no API key, no pip install.

Python's standard library only, so this runs on any Python 3.8+ with nothing
installed. It exists so you can see real results before setting anything up; the
full package (``creator-contacts``) does the same thing plus YouTube, link
following and a database.

Why podcasts work with no key: the podcast RSS spec requires
``<itunes:owner><itunes:email>`` for directory submission, so every show in Apple
Podcasts has published a contact address on purpose, in a machine-readable field.

Usage
-----
    python3 scripts/podcast_contacts.py                         # Hindi podcasts
    python3 scripts/podcast_contacts.py --term "standup comedy" --country IN
    python3 scripts/podcast_contacts.py --gmail-only --target 100
    python3 scripts/podcast_contacts.py --term "hindi business" --limit 40 -o out.csv

``--target N`` stops as soon as N contacts are collected, so it does not read
feeds it does not need. Because not every show publishes a usable address, give
``--limit`` plenty of headroom above the target — roughly double is a safe start.

Scope: this collects the show's published owner/business email only. It does not
touch personal phone numbers or home addresses — see docs/SCOPE.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

UA = "creator-contacts-demo/0.1 (+https://github.com/TalibKhan-Newbie/claudecode)"
DELAY = 1.0  # seconds between requests — be a good citizen
TIMEOUT = 20
MAX_FEED_BYTES = 3_000_000

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9](?:[A-Za-z0-9._%+\-]{0,62}[A-Za-z0-9])?"
    r"@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9\-]{1,63})*\.[A-Za-z]{2,24}\b"
)

NOISE_DOMAINS = {
    "example.com", "example.org", "domain.com", "yourdomain.com", "email.com",
    "youtube.com", "google.com", "libsyn.com", "megaphone.fm", "anchor.fm",
    "spotify.com", "buzzsprout.com", "podbean.com", "spreaker.com",
    "simplecast.com", "transistor.fm", "captivate.fm", "redcircle.com",
}
NOISE_LOCALS = {
    "noreply", "no-reply", "donotreply", "support", "help", "info@example",
    "you", "your", "youremail", "name", "email", "test", "user", "podcast",
}

BUSINESS_WORDS = (
    "business", "enquir", "inquir", "collab", "brand", "sponsor", "advert",
    "partner", "media", "press", "booking", "management", "manager",
)

#: What --gmail-only accepts. googlemail.com is the same mailbox as gmail.com.
GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def email_domain(address: str) -> str:
    """Lowercased domain, or '' when there is no '@' (rpartition would else
    return the whole string as the domain)."""
    _, at, domain = address.rpartition("@")
    return domain.strip().lower() if at else ""


def fetch(url: str, params: dict | None = None) -> bytes:
    """GET a URL with a polite UA and a size cap. Returns b'' on failure."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read(MAX_FEED_BYTES)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        print(f"    ! fetch failed: {type(exc).__name__}", file=sys.stderr)
        return b""


def plausible_email(email: str) -> bool:
    if email.count("@") != 1 or ".." in email:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or len(email) > 254:
        return False
    if re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", domain, re.I):
        return False
    if domain.lower() in NOISE_DOMAINS or local.lower() in NOISE_LOCALS:
        return False
    tld = domain.rsplit(".", 1)[-1]
    return tld.isalpha() and len(tld) >= 2


def search_podcasts(term: str, country: str, limit: int) -> list[dict]:
    """Search the free iTunes directory. No key required."""
    raw = fetch(
        ITUNES_SEARCH,
        {
            "media": "podcast",
            "entity": "podcast",
            "term": term,
            "country": country,
            "limit": min(limit, 200),
        },
    )
    if not raw:
        return []
    try:
        return json.loads(raw.decode("utf-8", "replace")).get("results", [])
    except ValueError:
        print("    ! could not parse the directory response", file=sys.stderr)
        return []


def text_of(node, path: str) -> str:
    found = node.find(path)
    if found is None:
        return ""
    return (found.text or "").strip()


def parse_feed(xml_bytes: bytes) -> dict:
    """Pull the contact fields out of one podcast RSS feed."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return {}

    channel = root.find("channel")
    if channel is None:
        return {}

    owner = channel.find(f"{ITUNES_NS}owner")
    owner_email = text_of(owner, f"{ITUNES_NS}email") if owner is not None else ""
    owner_name = text_of(owner, f"{ITUNES_NS}name") if owner is not None else ""

    description = text_of(channel, "description") or text_of(channel, f"{ITUNES_NS}summary")

    # Fall back to any email in the description if the owner block is absent.
    body_email = ""
    if not owner_email and description:
        for candidate in EMAIL_RE.findall(description):
            if plausible_email(candidate.lower()):
                body_email = candidate.lower()
                break

    return {
        "title": text_of(channel, "title"),
        "author": text_of(channel, f"{ITUNES_NS}author") or owner_name,
        "owner_name": owner_name,
        "owner_email": owner_email.lower().strip(),
        "body_email": body_email,
        "website": text_of(channel, "link"),
        "language": text_of(channel, "language"),
        "description": " ".join(description.split())[:300],
        "episodes": len(channel.findall("item")),
    }


def is_business_email(email: str, context: str) -> bool:
    blob = f"{email} {context}".lower()
    return any(word in blob for word in BUSINESS_WORDS)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find podcast business contacts. No API key needed."
    )
    parser.add_argument("--term", "-t", default="hindi podcast", help="Search term.")
    parser.add_argument("--country", "-c", default="IN", help="Store country code.")
    parser.add_argument("--limit", "-l", type=int, default=25, help="Shows to check.")
    parser.add_argument("-o", "--out", default="podcast_contacts.csv", help="Output CSV.")
    parser.add_argument(
        "--gmail-only",
        action="store_true",
        help="Keep only gmail.com / googlemail.com addresses.",
    )
    parser.add_argument(
        "--email-domain",
        action="append",
        metavar="DOMAIN",
        help="Keep only these domains. Repeatable. Overrides --gmail-only.",
    )
    parser.add_argument(
        "--target",
        "-n",
        type=int,
        default=None,
        help="Stop once this many contacts are collected.",
    )
    args = parser.parse_args()

    if args.email_domain:
        allowed = frozenset(d.strip().lower().lstrip("@") for d in args.email_domain if d.strip())
    elif args.gmail_only:
        allowed = GMAIL_DOMAINS
    else:
        allowed = frozenset()

    if allowed:
        print(f"Email filter: only {', '.join(sorted(allowed))}")
    if args.target and args.limit < args.target * 2:
        print(
            f"Note: --limit {args.limit} is tight for a target of {args.target}; "
            f"not every show publishes an address. Consider --limit {args.target * 2}."
        )

    print(f"Searching Apple Podcasts for {args.term!r} in {args.country}…")
    shows = search_podcasts(args.term, args.country, args.limit)
    if not shows:
        print(
            "\nNo results. Either the network blocked itunes.apple.com, or the term "
            "found nothing. Try --term 'comedy'.",
            file=sys.stderr,
        )
        return 1

    print(f"Found {len(shows)} shows. Reading their RSS feeds…\n")

    rows: list[dict] = []
    for index, show in enumerate(shows, start=1):
        name = show.get("collectionName") or show.get("trackName") or "?"
        feed_url = show.get("feedUrl")
        print(f"[{index}/{len(shows)}] {name[:58]}")

        if not feed_url:
            print("    - no feed listed")
            continue

        time.sleep(DELAY)
        feed = parse_feed(fetch(feed_url))
        if not feed:
            print("    - feed unreadable")
            continue

        email = feed["owner_email"] or feed["body_email"]
        if not (email and plausible_email(email)):
            print("    - no usable email published")
            continue

        if allowed and email_domain(email) not in allowed:
            print(f"    - skipped, {email_domain(email)} not in the domain filter")
            continue

        source = "itunes:owner/itunes:email" if feed["owner_email"] else "show description"
        business = is_business_email(email, feed["description"])
        genres = show.get("genres") or []

        rows.append(
            {
                "name": feed["title"] or name,
                "host": feed["author"] or show.get("artistName", ""),
                "email": email,
                "email_is_business": "yes" if business else "",
                "email_source": source,
                "email_source_url": feed_url,
                "website": feed["website"],
                "language": feed["language"],
                "episodes": feed["episodes"],
                "genres": ", ".join(str(g) for g in genres[:3]),
                "apple_url": show.get("collectionViewUrl", ""),
                "fields_found": 2 + int(bool(feed["website"])),
            }
        )
        flag = " [business]" if business else ""
        counter = f"  ({len(rows)}/{args.target})" if args.target else ""
        print(f"    OK  {email}{flag}{counter}")

        if args.target and len(rows) >= args.target:
            print(f"\nTarget of {args.target} reached — stopping.")
            break

    if not rows:
        hint = (
            " The domain filter may be too strict — many creators use a custom domain."
            if allowed
            else " Try a different --term."
        )
        print(f"\nNo contacts found.{hint}", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: (r["email_is_business"] != "yes", r["name"].lower()))

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    business_count = sum(1 for r in rows if r["email_is_business"] == "yes")
    print(f"\n{'=' * 58}")
    print(f"{len(rows)} contacts written to {args.out}")
    print(f"  {business_count} look explicitly business/collab addresses")
    print(f"  {len(rows) - business_count} are general contact addresses")
    if args.target and len(rows) < args.target:
        print(
            f"  {len(rows)}/{args.target} of your target — raise --limit or try "
            "another --term to find more."
        )
    print("\nEvery row carries email_source_url — keep it, so you can always")
    print("show where an address came from.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
