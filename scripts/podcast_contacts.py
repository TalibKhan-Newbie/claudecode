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
    # one-off
    python3 scripts/podcast_contacts.py --term "standup comedy" --country IN

    # several terms; a show matching two is read only once
    python3 scripts/podcast_contacts.py -t "hindi podcast" -t "desi startup"

    # keyword list in a file, keep going until you stop it
    python3 scripts/podcast_contacts.py --terms-file keywords.txt --loop \
        --gmail-only --target 100

Keyword file: one search term per line. Blank lines and ``#`` comments ignored.
It is re-read at the start of every cycle, so you can add terms while the loop
is running without restarting it.

Loop mode stops on Ctrl+C (saving first), when ``--target`` is met, or after
``--max-cycles``. The CSV is saved after every term, so an interrupt never costs
more than the term in flight.

Honest note on looping: Apple's directory returns the same shows for the same
term, so a second pass over an unchanged keyword list finds nothing new until
shows are actually published. The loop earns its keep on a long keyword list, or
with a long ``--interval`` to pick up new shows over days.

Scope: this collects a show's published owner/business email only. It does not
touch personal phone numbers or home addresses — see docs/SCOPE.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
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

#: Fixed CSV columns, so appending into an older file cannot shuffle or drop any.
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
    "found_via",
    "apple_url",
    "fields_found",
]

#: Set by the Ctrl+C handler; checked between requests so a stop is quick but
#: never leaves a half-written file.
_stop_requested = False


def _handle_interrupt(signum, frame) -> None:  # noqa: ARG001
    global _stop_requested
    if _stop_requested:
        # Second Ctrl+C — the user means it.
        print("\nForced exit.", file=sys.stderr)
        raise SystemExit(130)
    _stop_requested = True
    print("\n\nStopping after this show — saving what is collected…", file=sys.stderr)


# -- helpers ----------------------------------------------------------------


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


def email_domain(address: str) -> str:
    """Lowercased domain, or '' when there is no '@'.

    ``rpartition`` puts the whole string in the tail when the separator is
    missing, so its presence is checked rather than assumed.
    """
    _, at, domain = address.rpartition("@")
    return domain.strip().lower() if at else ""


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


def is_business_email(email: str, context: str) -> bool:
    blob = f"{email} {context}".lower()
    return any(word in blob for word in BUSINESS_WORDS)


def load_terms(path: str | None, inline: list[str] | None) -> list[str]:
    """Search terms from ``path`` (one per line, ``#`` comments) plus ``inline``.

    Re-read every cycle so the file can be edited while a loop runs. A missing or
    unreadable file yields nothing rather than stopping the run.
    """
    terms: list[str] = []
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    term = line.split("#", 1)[0].strip()
                    if term:
                        terms.append(term)
        except OSError as exc:
            print(f"! could not read {path}: {type(exc).__name__}", file=sys.stderr)
    terms.extend(inline or [])

    deduped: dict[str, None] = {}
    for term in terms:
        deduped.setdefault(term, None)
    return list(deduped)


def load_existing(path: str) -> list[dict]:
    """Rows already in ``path``, so a run can merge instead of replacing.

    A missing or unreadable file is treated as empty — resuming should never be
    the thing that loses the data.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return [row for row in csv.DictReader(handle) if row.get("email")]
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        print(
            f"! could not read {path} ({type(exc).__name__}), starting fresh",
            file=sys.stderr,
        )
        return []


def save_rows(rows: list[dict], path: str) -> None:
    """Write ``rows`` atomically, business addresses first.

    Writes a temp file and replaces, so a crash or Ctrl+C mid-write cannot leave
    a truncated CSV.
    """
    ordered = sorted(
        rows,
        key=lambda r: (r.get("email_is_business", "") != "yes", r.get("name", "").lower()),
    )
    temp = f"{path}.tmp"
    with open(temp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in ordered:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})
    os.replace(temp, path)


# -- directory + feeds ------------------------------------------------------


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


def row_from(show: dict, feed: dict, feed_url: str, email: str, term: str) -> dict:
    source = "itunes:owner/itunes:email" if feed["owner_email"] else "show description"
    genres = show.get("genres") or []
    return {
        "name": feed["title"] or show.get("collectionName", ""),
        "host": feed["author"] or show.get("artistName", ""),
        "email": email,
        "email_domain": email_domain(email),
        "email_is_business": "yes" if is_business_email(email, feed["description"]) else "",
        "email_source": source,
        "email_source_url": feed_url,
        "website": feed["website"],
        "language": feed["language"],
        "episodes": feed["episodes"],
        "genres": ", ".join(str(g) for g in genres[:3]),
        "found_via": term,
        "apple_url": show.get("collectionViewUrl", ""),
        "fields_found": 2 + int(bool(feed["website"])),
    }


def process_term(
    term: str,
    *,
    country: str,
    limit: int,
    allowed: frozenset[str],
    seen_emails: set[str],
    seen_feeds: set[str],
    rows: list[dict],
    target: int | None,
) -> int:
    """Search one term and append any new contacts to ``rows``. Returns count added."""
    print(f"\n[{term}] searching…")
    shows = search_podcasts(term, country, limit)
    if not shows:
        print("    no results")
        return 0

    fresh = [s for s in shows if s.get("feedUrl") and s["feedUrl"] not in seen_feeds]
    print(f"    {len(shows)} shows, {len(fresh)} not yet checked")

    added = 0
    for show in fresh:
        if _stop_requested:
            break
        if target and len(rows) >= target:
            break

        feed_url = show["feedUrl"]
        seen_feeds.add(feed_url)
        name = show.get("collectionName") or show.get("trackName") or "?"

        time.sleep(DELAY)
        feed = parse_feed(fetch(feed_url))
        if not feed:
            continue

        email = feed["owner_email"] or feed["body_email"]
        if not (email and plausible_email(email)):
            continue
        if allowed and email_domain(email) not in allowed:
            continue
        if email in seen_emails:
            continue

        seen_emails.add(email)
        rows.append(row_from(show, feed, feed_url, email, term))
        added += 1

        flag = " [business]" if rows[-1]["email_is_business"] else ""
        counter = f"  ({len(rows)}/{target})" if target else f"  ({len(rows)})"
        print(f"    OK  {name[:36]:38} {email}{flag}{counter}")

    if not added:
        print("    nothing new")
    return added


def interruptible_sleep(seconds: int) -> None:
    """Sleep, but wake immediately on Ctrl+C."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not _stop_requested:
        remaining = int(end - time.monotonic())
        print(f"\r  next cycle in {remaining:5d}s  (Ctrl+C to stop)", end="", flush=True)
        time.sleep(min(1.0, max(end - time.monotonic(), 0)))
    print("\r" + " " * 50 + "\r", end="")


# -- main -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find podcast business contacts. No API key needed.",
        epilog="Keyword file: one term per line, '#' for comments. Re-read each cycle.",
    )
    parser.add_argument(
        "--term", "-t", action="append", metavar="TERM",
        help="Search term. Repeatable.",
    )
    parser.add_argument(
        "--terms-file", "-f", metavar="PATH",
        help="File of search terms, one per line.",
    )
    parser.add_argument("--country", "-c", default="IN", help="Store country code.")
    parser.add_argument("--limit", "-l", type=int, default=50, help="Shows per term.")
    parser.add_argument("-o", "--out", default="podcast_contacts.csv", help="Output CSV.")
    parser.add_argument(
        "--gmail-only", action="store_true",
        help="Keep only gmail.com / googlemail.com addresses.",
    )
    parser.add_argument(
        "--email-domain", action="append", metavar="DOMAIN",
        help="Keep only these domains. Repeatable. Overrides --gmail-only.",
    )
    parser.add_argument(
        "--target", "-n", type=int, default=None,
        help="Stop once this many contacts are held.",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Keep cycling the terms until Ctrl+C, the target, or --max-cycles.",
    )
    parser.add_argument(
        "--interval", type=int, default=1800,
        help="Seconds to wait between cycles in --loop mode (default 1800).",
    )
    parser.add_argument(
        "--max-cycles", type=int, default=None,
        help="Stop after this many cycles. Default: unlimited.",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Ignore and overwrite an existing CSV instead of resuming from it.",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_interrupt)

    if args.email_domain:
        allowed = frozenset(d.strip().lower().lstrip("@") for d in args.email_domain if d.strip())
    elif args.gmail_only:
        allowed = GMAIL_DOMAINS
    else:
        allowed = frozenset()

    terms = load_terms(args.terms_file, args.term)
    if not terms:
        if args.terms_file:
            print(
                f"No usable terms in {args.terms_file}. "
                "Put one search term per line, then run again.",
                file=sys.stderr,
            )
            return 2
        terms = ["hindi podcast"]

    # Resume from the existing CSV by default, so a stopped run can be continued.
    rows: list[dict] = []
    seen_emails: set[str] = set()
    seen_feeds: set[str] = set()

    if not args.fresh:
        existing = load_existing(args.out)
        rows.extend(existing)
        for row in existing:
            if row.get("email"):
                seen_emails.add(row["email"].lower())
            if row.get("email_source_url"):
                seen_feeds.add(row["email_source_url"])
        if existing:
            print(f"Resuming {args.out} — {len(existing)} contacts already collected.")
    elif os.path.exists(args.out):
        print(f"--fresh: {args.out} will be overwritten.")

    print(f"Terms: {len(terms)}  |  country: {args.country}  |  limit/term: {args.limit}")
    if allowed:
        print(f"Email filter: only {', '.join(sorted(allowed))}")
    if args.target:
        print(f"Target: {args.target} contacts")
    if args.loop:
        print(f"Loop mode: cycling every {args.interval}s. Press Ctrl+C to stop.")

    start_total = len(rows)
    cycle = 0

    while not _stop_requested:
        cycle += 1
        cycle_terms = load_terms(args.terms_file, args.term) or terms
        print(f"\n{'=' * 60}\nCycle {cycle} — {len(cycle_terms)} term(s)")

        cycle_added = 0
        for term in cycle_terms:
            if _stop_requested:
                break
            if args.target and len(rows) >= args.target:
                break

            cycle_added += process_term(
                term,
                country=args.country,
                limit=args.limit,
                allowed=allowed,
                seen_emails=seen_emails,
                seen_feeds=seen_feeds,
                rows=rows,
                target=args.target,
            )
            # Save after every term, so an interrupt costs at most one term.
            if rows:
                save_rows(rows, args.out)

        print(f"\nCycle {cycle}: +{cycle_added} new, {len(rows)} total")

        if args.target and len(rows) >= args.target:
            print(f"\nTarget of {args.target} reached.")
            break
        if not args.loop:
            break
        if args.max_cycles and cycle >= args.max_cycles:
            print(f"\nReached --max-cycles {args.max_cycles}.")
            break
        if cycle_added == 0:
            print(
                "  (no new contacts — the directory returns the same shows for the "
                "same terms, so add terms to your file or use a longer --interval)"
            )
        if _stop_requested:
            break
        interruptible_sleep(args.interval)

    if rows:
        save_rows(rows, args.out)

    added = len(rows) - start_total
    business = sum(1 for r in rows if r.get("email_is_business") == "yes")

    print(f"\n{'=' * 60}")
    if _stop_requested:
        print("Stopped by you.")
    print(f"{len(rows)} contacts in {args.out}  (+{added} this session, {cycle} cycle(s))")
    if rows:
        print(f"  {business} look explicitly business/collab addresses")
        print(f"  {len(rows) - business} are general contact addresses")
    if args.target and len(rows) < args.target:
        print(f"  {len(rows)}/{args.target} of target — add more terms to your keyword file.")
    if not rows:
        hint = (
            "The domain filter may be too strict — many creators use a custom domain."
            if allowed
            else "Try different terms."
        )
        print(f"No contacts found. {hint}", file=sys.stderr)
        return 1

    print("\nRe-run the same command any time — it resumes from the CSV and skips")
    print("what it already has. Every row carries email_source_url.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
