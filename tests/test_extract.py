"""Extraction tests. No network — every input is a literal."""

from __future__ import annotations

import pytest

from creatorcontacts.extract import (
    extract_emails,
    extract_from_html,
    extract_from_jsonld,
    extract_labelled_address,
    extract_outbound_links,
    extract_phones,
    is_link_hub,
    is_plausible_email,
)
from creatorcontacts.models import ContactKind, SourceType

DESC = SourceType.CHANNEL_DESCRIPTION
CONTACT = SourceType.CONTACT_PAGE


def kinds(points, kind):
    return [p for p in points if p.kind is kind]


def values(points, kind):
    return {p.normalized for p in points if p.kind is kind}


# -- emails -----------------------------------------------------------------


def test_finds_plain_email_in_channel_description():
    text = "New video every Friday!\nFor business enquiries: teamrahul@gmail.com\nThanks"
    found = extract_emails(text, source_url="https://youtube.com/@x", source_type=DESC)
    assert [p.normalized for p in found] == ["teamrahul@gmail.com"]


def test_business_marker_raises_confidence_and_flags_business():
    business = extract_emails(
        "Business enquiries: hi@example.in", source_url="u", source_type=DESC
    )[0]
    casual = extract_emails("mail me at hi@example.in", source_url="u", source_type=DESC)[0]

    assert business.is_business is True
    assert casual.is_business is False
    assert business.confidence > casual.confidence


def test_snippet_captures_surrounding_context_for_audit():
    text = "Subscribe now. For collabs contact manager@agency.co.in only. Bye."
    point = extract_emails(text, source_url="u", source_type=DESC)[0]
    assert "collabs" in point.evidence.snippet
    assert point.evidence.source_url == "u"


def test_multiple_emails_all_returned_ranked():
    text = "Business: biz@studio.in | Personal: me@gmail.com"
    found = extract_emails(text, source_url="u", source_type=DESC)
    assert {p.normalized for p in found} == {"biz@studio.in", "me@gmail.com"}
    # The business-marked, non-freemail address should rank first.
    assert found[0].normalized == "biz@studio.in"


@pytest.mark.parametrize(
    "junk",
    [
        "logo@2x.png",
        "noreply@youtube.com",
        "you@example.com",
        "name@domain.com",
        "sentry@sentry.io",
    ],
)
def test_rejects_boilerplate_and_asset_lookalikes(junk):
    found = extract_emails(f"contact {junk} now", source_url="u", source_type=DESC)
    assert found == []


def test_is_plausible_email_edge_cases():
    assert is_plausible_email("a@b.co")
    assert not is_plausible_email("no-at-sign.com")
    assert not is_plausible_email("two@@at.com")
    assert not is_plausible_email("dots@in..domain.com")
    assert not is_plausible_email("a@b.c")  # single-char TLD


# -- obfuscation ------------------------------------------------------------


def test_obfuscated_email_skipped_by_default():
    text = "Business: rahul [at] gmail [dot] com"
    assert extract_emails(text, source_url="u", source_type=DESC) == []


def test_obfuscated_email_found_when_opted_in():
    text = "Business: rahul [at] gmail [dot] com"
    found = extract_emails(
        text, source_url="u", source_type=DESC, respect_obfuscation=False
    )
    assert [p.normalized for p in found] == ["rahul@gmail.com"]
    assert found[0].was_obfuscated is True


def test_obfuscated_email_scores_lower_than_plain():
    plain = extract_emails("hi rahul@gmail.com", source_url="u", source_type=DESC)[0]
    obfuscated = extract_emails(
        "hi rahul (at) gmail (dot) com",
        source_url="u",
        source_type=DESC,
        respect_obfuscation=False,
    )[0]
    assert obfuscated.confidence < plain.confidence


@pytest.mark.parametrize(
    "prose",
    [
        "Meet me at the dot com boom reunion",
        "I was at work dot com era startups",
        "Look at my dot com portfolio",
        "We are at our dot com peak",
        "based at the dot in office",
    ],
)
def test_prose_at_dot_does_not_become_an_email(prose):
    """Regression: bare 'at ... dot com' in a sentence produced me@the.com."""
    assert (
        extract_emails(prose, source_url="u", source_type=DESC, respect_obfuscation=False)
        == []
    )


def test_bracketed_obfuscation_survives_the_stopword_guard():
    """Prose never writes '[at]', so brackets bypass the stopword check."""
    found = extract_emails(
        "Business: me [at] studiokaam [dot] in",
        source_url="u",
        source_type=DESC,
        respect_obfuscation=False,
    )
    assert [p.normalized for p in found] == ["me@studiokaam.in"]


@pytest.mark.parametrize(
    "variant",
    [
        "rahul [at] gmail [dot] com",
        "rahul (at) gmail (dot) com",
        "rahul AT gmail DOT com",
        "rahul{at}gmail{dot}com",
    ],
)
def test_obfuscation_variants_all_resolve(variant):
    found = extract_emails(
        f"Business: {variant}", source_url="u", source_type=DESC, respect_obfuscation=False
    )
    assert [p.normalized for p in found] == ["rahul@gmail.com"]


# -- phones -----------------------------------------------------------------


def test_phone_ignored_in_free_prose_source():
    """A phone in a channel description is not business contact info."""
    text = "Call 098765 43210 for bookings"
    assert extract_phones(text, source_url="u", source_type=DESC) == []


def test_phone_accepted_from_contact_page():
    text = "Office: +91 98765 43210"
    found = extract_phones(text, source_url="u", source_type=CONTACT, region="IN")
    assert [p.normalized for p in found] == ["+919876543210"]


def test_phone_from_tel_link_is_marked_business():
    html = '<a href="tel:+919876543210">Call us</a>'
    points = extract_from_html(html, source_url="u", source_type=CONTACT)
    phones = kinds(points, ContactKind.PHONE)
    assert phones and phones[0].is_business is True


def test_invalid_number_sequences_rejected():
    # Timestamps and view counts must not survive validation.
    text = "Office: 00:12:34 and 1234567 views and 2024-01-01"
    found = extract_phones(text, source_url="u", source_type=CONTACT, region="IN")
    assert found == []


# -- addresses --------------------------------------------------------------


def test_address_requires_explicit_label():
    unlabelled = "We are somewhere in Andheri West Mumbai 400053"
    assert extract_labelled_address(unlabelled, source_url="u", source_type=CONTACT) == []


def test_labelled_address_extracted_from_contact_page():
    text = "Office Address: 21 Link Road, Andheri West, Mumbai 400053"
    found = extract_labelled_address(text, source_url="u", source_type=CONTACT)
    assert len(found) == 1
    assert "Link Road" in found[0].normalized
    assert found[0].is_business is True


def test_address_never_taken_from_prose_source():
    text = "Registered office: 21 Link Road, Mumbai 400053"
    assert extract_labelled_address(text, source_url="u", source_type=DESC) == []


def test_address_needs_a_digit():
    text = "Address: somewhere nice indeed"
    assert extract_labelled_address(text, source_url="u", source_type=CONTACT) == []


# -- structured markup ------------------------------------------------------

JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Organization",
  "name": "Studio Kaam",
  "email": "mailto:hello@studiokaam.in",
  "telephone": "+91 22 4000 1234",
  "address": {
    "@type": "PostalAddress",
    "streetAddress": "21 Link Road",
    "addressLocality": "Mumbai",
    "postalCode": "400053",
    "addressCountry": "IN"
  }
}
</script>
</head><body></body></html>
"""


def test_jsonld_yields_email_phone_and_address():
    points = extract_from_jsonld(JSONLD_PAGE, source_url="https://studiokaam.in/contact")
    assert values(points, ContactKind.EMAIL) == {"hello@studiokaam.in"}
    assert values(points, ContactKind.PHONE) == {"+912240001234"}
    address = kinds(points, ContactKind.ADDRESS)[0]
    assert "21 Link Road" in address.normalized
    assert "400053" in address.normalized
    assert all(p.is_business for p in points)


def test_jsonld_nested_in_graph_is_found():
    page = """
    <script type="application/ld+json">
    {"@graph": [{"@type": "WebSite"}, {"@type": "Person", "email": "a@b.in"}]}
    </script>
    """
    points = extract_from_jsonld(page, source_url="u")
    assert values(points, ContactKind.EMAIL) == {"a@b.in"}


def test_malformed_jsonld_does_not_raise():
    page = '<script type="application/ld+json">{ not json at all }</script>'
    assert extract_from_jsonld(page, source_url="u") == []


# -- html -------------------------------------------------------------------


def test_mailto_link_beats_body_text_confidence():
    html = """
    <html><body>
      <p>random text with backup@gmail.com in it</p>
      <a href="mailto:business@studio.in">Business enquiries</a>
    </body></html>
    """
    points = extract_from_html(html, source_url="https://studio.in/contact", source_type=CONTACT)
    emails = {p.normalized: p for p in kinds(points, ContactKind.EMAIL)}
    assert "business@studio.in" in emails
    assert emails["business@studio.in"].evidence.source_type is SourceType.MAILTO_LINK
    assert emails["business@studio.in"].confidence > emails["backup@gmail.com"].confidence


def test_script_and_style_content_is_ignored():
    html = """
    <html><body>
      <script>var t = "tracker@analytics.io";</script>
      <style>/* css@nowhere.com */</style>
      <p>real: hi@real.in</p>
    </body></html>
    """
    points = extract_from_html(html, source_url="u", source_type=CONTACT)
    assert values(points, ContactKind.EMAIL) == {"hi@real.in"}


def test_percent_encoded_mailto_is_decoded():
    html = '<a href="mailto:a%40b.in?subject=Hi">mail</a>'
    points = extract_from_html(html, source_url="u", source_type=CONTACT)
    assert values(points, ContactKind.EMAIL) == {"a@b.in"}


# -- link hubs --------------------------------------------------------------


def test_outbound_links_exclude_same_host_and_relative():
    html = """
    <a href="https://linktr.ee/self">self</a>
    <a href="/relative">rel</a>
    <a href="https://mysite.in">site</a>
    <a href="https://instagram.com/me">ig</a>
    """
    links = extract_outbound_links(html, base_url="https://linktr.ee/me")
    assert "https://mysite.in" in links
    assert "https://instagram.com/me" in links
    assert not any("linktr.ee" in link for link in links)
    assert not any(link.startswith("/") for link in links)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://linktr.ee/someone", True),
        ("https://beacons.ai/someone", True),
        ("https://www.bio.link/someone", True),
        ("https://mysite.in/links", False),
    ],
)
def test_is_link_hub(url, expected):
    assert is_link_hub(url) is expected
