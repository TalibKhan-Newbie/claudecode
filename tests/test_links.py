"""Tests for link-following, and for the hosts we refuse to touch.

The ``should_skip`` tests are not cosmetic. Blocking people-search and data-broker
domains at the network layer is what keeps this an outreach tool rather than a
people-finder, so those assertions are load-bearing — see ``docs/SCOPE.md``.
"""

from __future__ import annotations

import pytest

from creatorcontacts.sources.links import SKIP_HOSTS, should_skip


@pytest.mark.parametrize(
    "url",
    [
        "https://truecaller.com/search/in/9876543210",
        "https://www.whitepages.com/name/John-Doe",
        "https://spokeo.com/John-Doe",
        "https://beenverified.com/people/john-doe",
        "https://fastpeoplesearch.com/name/john",
        "https://truepeoplesearch.com/results?name=john",
        "https://radaris.com/p/John/Doe/",
        "https://thatsthem.com/name/john-doe",
        "https://instantcheckmate.com/people/john-doe",
        "https://usphonebook.com/9876543210",
    ],
)
def test_people_search_and_broker_hosts_are_never_fetched(url):
    assert should_skip(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com/someone",
        "https://www.facebook.com/someone",
        "https://linkedin.com/in/someone",
        "https://tiktok.com/@someone",
        "https://x.com/someone",
        "https://twitter.com/someone",
    ],
)
def test_platforms_that_forbid_scraping_are_skipped(url):
    assert should_skip(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://studiokaam.in",
        "https://www.studiokaam.in/contact",
        "https://rahul.co.in/work-with-me",
        "https://linktr.ee/rahul",
        "https://beacons.ai/rahul",
    ],
)
def test_creator_owned_sites_and_hubs_are_allowed(url):
    assert should_skip(url) is False


def test_subdomains_of_blocked_hosts_are_also_skipped():
    assert should_skip("https://api.truecaller.com/x") is True
    assert should_skip("https://in.linkedin.com/in/someone") is True


def test_lookalike_domain_is_not_accidentally_blocked():
    """`nottruecaller.in` must not match `truecaller.com`."""
    assert should_skip("https://nottruecaller.in") is False
    assert should_skip("https://mytruecaller-fan.in") is False


@pytest.mark.parametrize("url", ["", "not-a-url", "ftp://x.in", "/relative/path"])
def test_unparseable_urls_are_skipped(url):
    assert should_skip(url) is True


def test_skip_list_covers_the_broker_categories():
    """Guard against someone trimming the list without thinking."""
    for required in ("truecaller.com", "whitepages.com", "spokeo.com", "beenverified.com"):
        assert required in SKIP_HOSTS
