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
    python3 scripts/podcast_contacts.py -t "hindi podcast" -t "desi startup"
    python3 scripts/podcast_contacts.py --gmail-only --target 100 --limit 200
    python3 scripts/podcast_contacts.py -t "hindi business" --append

``--term`` is repeatable, and a show matching two terms is read only once.

The CSV is **replaced** by default, which is the usual behaviour for an output
file but will discard an earlier run's results. ``--append`` merges into the
existing file instead, deduplicating by email address, which is what you want
when building one list across several search terms.

``--target N`` stops as soon as N contacts are held, so it does not read feeds it
does not need. Because not every show publishes a usable address, give ``--limit``
plenty of headroom above the target — roughly double is a safe start.

Scope: this collects the show's published owner/business email only. It does not
touch personal phone numbers or home addresses — see docs/SCOPE.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
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

#: Fixed CSV columns, so --append into an older file cannot shuffle or drop any.
COLUMNS = [
    "name",
    "host",
    "email",
    "email_domain",
    "email_is_business",
    "email_source",
    "email_source_url",
    "website",
    "language",
    "episodes",
    "genres",
    "apple_url",
    "fields_found",
]


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


def load_existing(path: str) -> list[dict]:
    """Rows already in ``path``, so ``--append`` can merge instead of replacing.

    A missing or unreadable file is treated as empty — appending should never be
    the thing that loses the run.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return [row for row in csv.DictReader(handle) if row.get("email")]
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        print(f"    ! could not read {path} ({type(exc).__name__}), starting fresh", file=sys.stderr)
        return []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find podcast business contacts. No API key needed."
    )
    parser.add_argument(
        "--term",
        "-t",
        action="append",
        metavar="TERM",
        help="Search term. Repeatable: -t 'hindi podcast' -t 'standup comedy'.",
    )
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
    parser.add_argument(
        "--append",
        "-a",
        action="store_true",
        help="Merge into the existing CSV instead of replacing it. Deduped by email.",
    )
    args = parser.parse_args()

    terms = args.term or ["hindi podcast"]

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

    # Rows already on disk, so --append merges rather than replaces.
    rows: list[dict] = []
    seen_emails: set[str] = set()

    if args.append:
        existing = load_existing(args.out)
        rows.extend(existing)
        seen_emails.update(r.get("email", "").lower() for r in existing if r.get("email"))
        if existing:
            print(f"Appending to {args.out} — {len(existing)} contacts already there.")
    elif os.path.exists(args.out) and os.path.getsize(args.out) > 0:
        print(
            f"Note: {args.out} exists and will be REPLACED. "
            "Use --append to merge instead, or -o another-name.csv."
        )

    # Collect the shows for every term first, deduped by feed URL so a show that
    # matches two terms is only read once.
    shows: list[dict] = []
    seen_feeds: set[str] = set()
    for term in terms:
        print(f"Searching Apple Podcasts for {term!r} in {args.country}…")
        for show in search_podcasts(term, args.country, args.limit):
            feed = show.get("feedUrl")
            if feed and feed not in seen_feeds:
                seen_feeds.add(feed)
                shows.append(show)

    if not shows:
        print(
            "\nNo results. Either the network blocked itunes.apple.com, or the terms "
            "found nothing. Try --term 'comedy'.",
            file=sys.stderr,
        )
        return 1

    print(f"\nFound {len(shows)} unique shows across {len(terms)} term(s). Reading feeds…\n")

    start_count = len(rows)
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

        if email in seen_emails:
            print(f"    - duplicate, {email} already collected")
            continue
        seen_emails.add(email)

        source = "itunes:owner/itunes:email" if feed["owner_email"] else "show description"
        business = is_business_email(email, feed["description"])
        genres = show.get("genres") or []

        rows.append(
            {
                "name": feed["title"] or name,
                "host": feed["author"] or show.get("artistName", ""),
                "email": email,
                "email_domain": email_domain(email),
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

    added = len(rows) - start_count

    if not rows:
        hint = (
            " The domain filter may be too strict — many creators use a custom domain."
            if allowed
            else " Try a different --term."
        )
        print(f"\nNo contacts found.{hint}", file=sys.stderr)
        return 1

    if added == 0:
        print(f"\nNo new contacts this run. {args.out} left as it was ({len(rows)} rows).")
        return 0

    # Business addresses first, then alphabetical. .get() because appended rows
    # come from a CSV and may predate a column.
    rows.sort(key=lambda r: (r.get("email_is_business", "") != "yes", r.get("name", "").lower()))

    # A fixed column list, so appending to a file written by an older run cannot
    # shuffle columns or drop a field.
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})

    business_count = sum(1 for r in rows if r.get("email_is_business") == "yes")
    print(f"\n{'=' * 58}")
    print(f"{len(rows)} contacts in {args.out}  (+{added} new this run)")
    print(f"  {business_count} look explicitly business/collab addresses")
    print(f"  {len(rows) - business_count} are general contact addresses")
    if args.target and len(rows) < args.target:
        print(
            f"  {len(rows)}/{args.target} of your target — raise --limit, add another "
            "--term, or run again with --append."
        )
    print("\nEvery row carries email_source_url — keep it, so you can always")
    print("show where an address came from.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
