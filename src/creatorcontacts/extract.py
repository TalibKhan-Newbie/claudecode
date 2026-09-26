"""Pull contact values out of text and HTML.

Two rules shape everything here:

1. A phone number or postal address is only ever taken from a source in
   ``BUSINESS_ONLY_SOURCES`` — a ``tel:`` link, structured business markup, or a
   labelled contact page. Free prose is scanned for emails and nothing else.
2. An obfuscated email (``name [at] site dot com``) is a creator asking not to be
   harvested automatically. ``respect_obfuscation`` honours that by default.
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlparse

import phonenumbers
from bs4 import BeautifulSoup
from bs4.element import Tag

from .models import (
    BUSINESS_MARKERS,
    BUSINESS_ONLY_SOURCES,
    ContactKind,
    ContactPoint,
    Evidence,
    SourceType,
)

#: Any http(s) URL inside free text — used to lift a creator's links out of a
#: channel description or show notes.
URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9](?:[A-Za-z0-9._%+\-]{0,62}[A-Za-z0-9])?"
    r"@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,24}\b"
)

#: ``name (at) site (dot) com`` and friends. Group 1 = local, 2 = domain body,
#: 3 = TLD. Deliberately narrow so prose like "meet me at dot com" cannot match.
OBFUSCATED_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9._%+\-]{1,64})"
    r"\s*(?:\(|\[|\{)?\s*(?:at|@|AT)\s*(?:\)|\]|\})?\s*"
    r"([A-Za-z0-9\-]{1,63}(?:\s*(?:\(|\[|\{)?\s*(?:dot|\.)\s*(?:\)|\]|\})?\s*[A-Za-z0-9\-]{1,63})*?)"
    r"\s*(?:\(|\[|\{)?\s*(?:dot|\.)\s*(?:\)|\]|\})?\s*"
    r"([A-Za-z]{2,24})\b",
    re.IGNORECASE,
)

#: English function words that appear around a bare "at ... dot com" in ordinary
#: prose ("meet me at the dot com boom"). When a creator obfuscates deliberately
#: they bracket the separators, so the stopword guard only applies to the
#: unbracketed form — see ``extract_emails``.
_OBFUSCATION_STOPWORDS = frozenset(
    {
        "a", "an", "the", "me", "my", "mine", "we", "us", "our", "ours", "you",
        "your", "yours", "he", "him", "his", "she", "her", "it", "its", "they",
        "them", "their", "this", "that", "these", "those", "and", "or", "but",
        "if", "then", "than", "so", "as", "is", "are", "am", "was", "were", "be",
        "been", "being", "to", "of", "in", "on", "by", "for", "with", "from",
        "up", "out", "off", "about", "into", "over", "under", "again", "all",
        "any", "both", "each", "few", "more", "most", "other", "some", "such",
        "no", "nor", "not", "only", "own", "same", "too", "very", "can", "will",
        "just", "should", "would", "could", "now", "here", "there", "when",
        "where", "why", "how", "what", "who", "whom", "which", "while",
        "because", "also", "really", "actually", "look", "looking", "meet",
        "meeting", "see", "seen", "get", "got", "go", "going", "come", "came",
        "back", "next", "last", "first", "one", "two", "live", "lives", "work",
        "working", "stay", "staying", "available", "based",
    }
)

#: Addresses are only read from text that labels itself as one.
ADDRESS_LABEL_RE = re.compile(
    r"(?:registered\s+office|office\s+address|mailing\s+address|postal\s+address"
    r"|studio\s+address|business\s+address|address)\s*[:\-–]\s*(.{12,220})",
    re.IGNORECASE | re.DOTALL,
)

#: Free-mail domains — a creator using one is normal, but it means the address
#: cannot be corroborated against the creator's own website domain.
FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.in",
        "yahoo.co.in",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "icloud.com",
        "me.com",
        "proton.me",
        "protonmail.com",
        "rediffmail.com",
        "zoho.com",
        "zohomail.in",
        "aol.com",
        "gmx.com",
        "mail.com",
        "yandex.com",
    }
)

#: Emails belonging to platforms/tools rather than to the creator.
NOISE_EMAIL_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "email.com",
        "domain.com",
        "yourdomain.com",
        "sentry.io",
        "wixpress.com",
        "squarespace.com",
        "shopify.com",
        "wordpress.com",
        "godaddy.com",
        "cloudflare.com",
        "youtube.com",
        "google.com",
        "facebook.com",
        "instagram.com",
        "support.google.com",
    }
)

NOISE_EMAIL_LOCALS = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "postmaster",
        "abuse",
        "webmaster",
        "hostmaster",
        "root",
        "admin@localhost",
        "you",
        "your",
        "youremail",
        "name",
        "email",
        "someone",
        "test",
        "user",
        "sentry",
    }
)

#: Asset filenames read as emails by the naive regex (``logo@2x.png``).
_ASSET_TAIL_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|css|js|woff2?|ico|mp4)$", re.I)

CONTACT_PAGE_PATHS = (
    "/contact",
    "/contact-us",
    "/contactus",
    "/contact-me",
    "/about",
    "/about-us",
    "/press",
    "/media",
    "/work-with-me",
    "/collab",
    "/collaborate",
    "/partnerships",
    "/business",
    "/booking",
    "/imprint",
    "/impressum",
)

LINK_HUB_HOSTS = frozenset(
    {
        "linktr.ee",
        "beacons.ai",
        "beacons.page",
        "bio.link",
        "linkin.bio",
        "campsite.bio",
        "taplink.cc",
        "solo.to",
        "carrd.co",
        "komi.io",
        "koji.to",
        "withkoji.com",
        "msha.ke",
        "lnk.bio",
        "allmylinks.com",
        "shor.by",
        "flowcode.com",
        "linkpop.com",
        "stan.store",
    }
)

_SNIPPET_PAD = 90


def _snippet(text: str, start: int, end: int, pad: int = _SNIPPET_PAD) -> str:
    lo = max(start - pad, 0)
    hi = min(end + pad, len(text))
    return " ".join(text[lo:hi].split())


def _has_business_marker(context: str) -> bool:
    lowered = context.lower()
    return any(marker in lowered for marker in BUSINESS_MARKERS)


def normalize_email(raw: str) -> str:
    return raw.strip().strip(".,;:<>()[]\"'").lower()


def is_plausible_email(email: str) -> bool:
    """Reject the regex's false positives without validating deliverability."""
    if email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or ".." in email:
        return False
    if len(email) > 254 or len(local) > 64:
        return False
    if _ASSET_TAIL_RE.search(domain):
        return False
    if domain in NOISE_EMAIL_DOMAINS:
        return False
    if local in NOISE_EMAIL_LOCALS:
        return False
    # ``logo@2x`` style asset references.
    if re.fullmatch(r"\d+x", domain.split(".")[0] or ""):
        return False
    tld = domain.rsplit(".", 1)[-1]
    return tld.isalpha() and len(tld) >= 2


def score_email(email: str, source_type: SourceType, is_business: bool, obfuscated: bool) -> float:
    base = {
        SourceType.PODCAST_RSS_OWNER: 0.95,
        SourceType.MAILTO_LINK: 0.9,
        SourceType.SCHEMA_ORG: 0.9,
        SourceType.CONTACT_PAGE: 0.82,
        SourceType.PROVIDER_API: 0.8,
        SourceType.CHANNEL_DESCRIPTION: 0.75,
        SourceType.PROFILE_BIO: 0.72,
        SourceType.LINK_HUB: 0.6,
        SourceType.WEBSITE_BODY: 0.55,
    }.get(source_type, 0.5)
    if is_business:
        base += 0.08
    domain = email.rpartition("@")[2]
    if domain not in FREEMAIL_DOMAINS:
        base += 0.04
    if obfuscated:
        base -= 0.1
    return round(min(max(base, 0.0), 0.99), 3)


def extract_emails(
    text: str,
    *,
    source_url: str,
    source_type: SourceType,
    respect_obfuscation: bool = True,
) -> list[ContactPoint]:
    """Every plausible email in ``text``, each with its surrounding snippet."""
    if not text:
        return []

    found: dict[str, ContactPoint] = {}

    for match in EMAIL_RE.finditer(text):
        email = normalize_email(match.group(0))
        if not is_plausible_email(email):
            continue
        context = _snippet(text, match.start(), match.end())
        is_business = _has_business_marker(context)
        point = ContactPoint(
            kind=ContactKind.EMAIL,
            value=email,
            normalized=email,
            is_business=is_business,
            confidence=score_email(email, source_type, is_business, obfuscated=False),
            evidence=Evidence(source_url=source_url, source_type=source_type, snippet=context),
        )
        prior = found.get(email)
        if prior is None or point.confidence > prior.confidence:
            found[email] = point

    if not respect_obfuscation:
        for match in OBFUSCATED_EMAIL_RE.finditer(text):
            local, domain_body, tld = match.groups()

            # Deliberate obfuscation brackets its separators ("name [at] site
            # [dot] com"); prose does not. Unbracketed matches are therefore
            # checked against the stopword list so that an ordinary sentence
            # like "meet me at the dot com boom" is not read as me@the.com.
            if not any(ch in match.group(0) for ch in "[](){}"):
                first_label = re.split(r"\s|\.", domain_body.strip(), maxsplit=1)[0]
                if (
                    local.lower() in _OBFUSCATION_STOPWORDS
                    or first_label.lower() in _OBFUSCATION_STOPWORDS
                ):
                    continue

            domain_body = re.sub(r"\s*(?:\(|\[|\{)?\s*(?:dot|\.)\s*(?:\)|\]|\})?\s*", ".", domain_body)
            email = normalize_email(f"{local}@{domain_body}.{tld}")
            if not is_plausible_email(email) or email in found:
                continue
            context = _snippet(text, match.start(), match.end())
            is_business = _has_business_marker(context)
            found[email] = ContactPoint(
                kind=ContactKind.EMAIL,
                value=email,
                normalized=email,
                is_business=is_business,
                was_obfuscated=True,
                confidence=score_email(email, source_type, is_business, obfuscated=True),
                evidence=Evidence(source_url=source_url, source_type=source_type, snippet=context),
            )

    return sorted(found.values(), key=lambda c: -c.confidence)


def extract_phones(
    text: str,
    *,
    source_url: str,
    source_type: SourceType,
    region: str | None = "IN",
) -> list[ContactPoint]:
    """Valid phone numbers, but only from a business-designated source.

    ``phonenumbers`` does the validation, so timestamps, view counts and video
    IDs do not survive. A source outside ``BUSINESS_ONLY_SOURCES`` returns
    nothing at all — see the module docstring.
    """
    if not text or source_type not in BUSINESS_ONLY_SOURCES:
        return []

    found: dict[str, ContactPoint] = {}
    for region_hint in (region, None) if region else (None,):
        for match in phonenumbers.PhoneNumberMatcher(text, region_hint):
            number = match.number
            if not phonenumbers.is_valid_number(number):
                continue
            e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
            if e164 in found:
                continue
            context = _snippet(text, match.start, match.start + len(match.raw_string))
            is_business = _has_business_marker(context) or source_type is SourceType.TEL_LINK
            confidence = 0.85 if source_type is SourceType.TEL_LINK else 0.7
            if is_business:
                confidence += 0.05
            found[e164] = ContactPoint(
                kind=ContactKind.PHONE,
                value=match.raw_string,
                normalized=e164,
                is_business=is_business,
                confidence=round(min(confidence, 0.95), 3),
                evidence=Evidence(source_url=source_url, source_type=source_type, snippet=context),
            )
    return sorted(found.values(), key=lambda c: -c.confidence)


def extract_labelled_address(
    text: str,
    *,
    source_url: str,
    source_type: SourceType,
) -> list[ContactPoint]:
    """Addresses that the page itself labels ``Address:`` / ``Registered office:``.

    Only runs for business-designated sources, and requires the label — we never
    guess that a blob of prose is an address.
    """
    if not text or source_type not in BUSINESS_ONLY_SOURCES:
        return []

    out: list[ContactPoint] = []
    for match in ADDRESS_LABEL_RE.finditer(text):
        body = " ".join(match.group(1).split())
        # Cut at the first sentence-ish boundary so we do not swallow the page.
        body = re.split(r"\s{2,}|\n|(?<=\d{6})\s|\||•", body)[0].strip(" .,;–-")
        if len(body) < 12 or not re.search(r"\d", body):
            continue
        out.append(
            ContactPoint(
                kind=ContactKind.ADDRESS,
                value=body,
                normalized=body,
                is_business=True,
                confidence=0.72,
                evidence=Evidence(
                    source_url=source_url,
                    source_type=source_type,
                    snippet=_snippet(text, match.start(), match.end()),
                ),
            )
        )
    return out


def _postal_address_to_line(node: dict) -> str:
    parts = [
        node.get("streetAddress"),
        node.get("addressLocality"),
        node.get("addressRegion"),
        node.get("postalCode"),
        node.get("addressCountry"),
    ]
    flat: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            part = part.get("name") or part.get("addressCountry")
        if isinstance(part, str) and part.strip():
            flat.append(" ".join(part.split()))
    return ", ".join(flat)


def _walk_jsonld(node: object):
    """Yield every dict in a JSON-LD tree, however deeply nested."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_jsonld(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def extract_from_jsonld(html: str, *, source_url: str) -> list[ContactPoint]:
    """Contact values from schema.org JSON-LD — the most reliable business source."""
    soup = BeautifulSoup(html, "html.parser")
    points: list[ContactPoint] = []
    evidence = Evidence(
        source_url=source_url,
        source_type=SourceType.SCHEMA_ORG,
        snippet="schema.org JSON-LD",
    )

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue

        for node in _walk_jsonld(data):
            email = node.get("email")
            if isinstance(email, str):
                cleaned = normalize_email(email.replace("mailto:", ""))
                if is_plausible_email(cleaned):
                    points.append(
                        ContactPoint(
                            kind=ContactKind.EMAIL,
                            value=cleaned,
                            normalized=cleaned,
                            is_business=True,
                            confidence=score_email(cleaned, SourceType.SCHEMA_ORG, True, False),
                            evidence=evidence,
                        )
                    )

            phone = node.get("telephone")
            if isinstance(phone, str):
                points.extend(
                    extract_phones(
                        phone,
                        source_url=source_url,
                        source_type=SourceType.SCHEMA_ORG,
                    )
                )

            address = node.get("address")
            for candidate in (address if isinstance(address, list) else [address]):
                if isinstance(candidate, dict) and candidate.get("@type") in (
                    "PostalAddress",
                    None,
                ):
                    line = _postal_address_to_line(candidate)
                    if len(line) >= 12:
                        points.append(
                            ContactPoint(
                                kind=ContactKind.ADDRESS,
                                value=line,
                                normalized=line,
                                is_business=True,
                                confidence=0.88,
                                evidence=evidence,
                            )
                        )
                elif isinstance(candidate, str) and len(candidate) >= 12:
                    points.append(
                        ContactPoint(
                            kind=ContactKind.ADDRESS,
                            value=" ".join(candidate.split()),
                            normalized=" ".join(candidate.split()),
                            is_business=True,
                            confidence=0.8,
                            evidence=evidence,
                        )
                    )
    return points


def _anchor_context(tag: Tag) -> str:
    own = tag.get_text(" ", strip=True)
    parent = tag.parent.get_text(" ", strip=True) if tag.parent else ""
    return " ".join(f"{own} {parent}".split())[: _SNIPPET_PAD * 3]


def extract_from_html(
    html: str,
    *,
    source_url: str,
    source_type: SourceType,
    region: str | None = "IN",
    respect_obfuscation: bool = True,
) -> list[ContactPoint]:
    """Everything extractable from one fetched page.

    ``mailto:`` and ``tel:`` anchors are the strongest signal on any page — the
    creator wired them up on purpose — so they are read before the body text.
    """
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "noscript", "template"]):
        junk.decompose()

    points: list[ContactPoint] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            email = normalize_email(unquote(href[7:].split("?")[0]))
            if is_plausible_email(email):
                context = _anchor_context(anchor)
                is_business = _has_business_marker(context) or _has_business_marker(email)
                points.append(
                    ContactPoint(
                        kind=ContactKind.EMAIL,
                        value=email,
                        normalized=email,
                        is_business=is_business,
                        confidence=score_email(email, SourceType.MAILTO_LINK, is_business, False),
                        evidence=Evidence(
                            source_url=source_url,
                            source_type=SourceType.MAILTO_LINK,
                            snippet=context or "mailto: link",
                        ),
                    )
                )
        elif low.startswith("tel:"):
            raw = unquote(href[4:])
            points.extend(
                extract_phones(
                    raw,
                    source_url=source_url,
                    source_type=SourceType.TEL_LINK,
                    region=region,
                )
            )

    points.extend(extract_from_jsonld(html, source_url=source_url))

    text = soup.get_text("\n", strip=True)
    points.extend(
        extract_emails(
            text,
            source_url=source_url,
            source_type=source_type,
            respect_obfuscation=respect_obfuscation,
        )
    )
    points.extend(extract_phones(text, source_url=source_url, source_type=source_type, region=region))
    points.extend(extract_labelled_address(text, source_url=source_url, source_type=source_type))
    return points


def extract_outbound_links(html: str, *, base_url: str, limit: int = 40) -> list[str]:
    """Outbound http(s) links from a page, for expanding a Linktree-style hub."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    seen: dict[str, None] = {}

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        host = urlparse(href).netloc.lower().removeprefix("www.")
        if not host or host == base_host:
            continue
        seen.setdefault(href.split("#")[0], None)
        if len(seen) >= limit:
            break
    return list(seen)


def is_link_hub(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host in LINK_HUB_HOSTS
