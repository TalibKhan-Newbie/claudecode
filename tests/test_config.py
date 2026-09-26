"""Config loading tests."""

from __future__ import annotations

from creatorcontacts.config import AppConfig

SAMPLE = """
database: mine.db
region: IN
language: hi

band:
  min_followers: 40000
  max_followers: 70000

crawl:
  delay_seconds: 3.0
  obey_robots: false

gate:
  min_fields: 3
  require_reachable: false

niches:
  comedy:
    - standup india
    - open mic delhi
  tech:
    - hindi tech review
"""


def write(tmp_path, text=SAMPLE, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_scalars_and_nested_sections_load(tmp_path):
    config = AppConfig.load(write(tmp_path))
    assert config.database == "mine.db"
    assert config.region == "IN"
    assert config.band.min_followers == 40_000
    assert config.band.max_followers == 70_000
    assert config.crawl.delay_seconds == 3.0
    assert config.crawl.obey_robots is False
    assert config.gate.min_fields == 3
    assert config.gate.require_reachable is False


def test_unset_options_keep_their_defaults(tmp_path):
    config = AppConfig.load(write(tmp_path))
    assert config.crawl.respect_obfuscation is True
    assert config.crawl.max_pages_per_creator == 8
    assert config.gate.min_confidence == 0.5


def test_missing_file_yields_defaults(tmp_path):
    config = AppConfig.load(tmp_path / "absent.yaml")
    assert config.database == "contacts.db"
    assert config.band.min_followers == 50_000
    assert config.band.max_followers == 60_000


def test_empty_file_yields_defaults(tmp_path):
    config = AppConfig.load(write(tmp_path, "", "empty.yaml"))
    assert config.database == "contacts.db"


def test_niche_terms(tmp_path):
    config = AppConfig.load(write(tmp_path))
    assert config.terms_for("comedy") == ["standup india", "open mic delhi"]
    assert config.all_niches() == ["comedy", "tech"]


def test_unknown_niche_falls_back_to_its_own_name(tmp_path):
    config = AppConfig.load(write(tmp_path))
    assert config.terms_for("cooking") == ["cooking"]


def test_unknown_option_is_ignored_not_fatal(tmp_path, caplog):
    config = AppConfig.load(write(tmp_path, "crawl:\n  nonsense_option: 5\n", "odd.yaml"))
    assert not hasattr(config.crawl, "nonsense_option")
    assert config.crawl.delay_seconds == 1.5


def test_user_agent_identifies_the_bot_and_mentions_robots(tmp_path):
    agent = AppConfig.load(write(tmp_path)).effective_user_agent()
    assert "creator-contacts" in agent
    assert "robots.txt" in agent


def test_explicit_user_agent_wins(tmp_path):
    config = AppConfig.load(write(tmp_path, 'crawl:\n  user_agent: "mybot/1.0"\n', "ua.yaml"))
    assert config.effective_user_agent() == "mybot/1.0"


def test_contact_url_comes_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_CONTACT_URL", "https://example.in/bot")
    config = AppConfig.load(write(tmp_path))
    assert config.contact_url == "https://example.in/bot"
    assert "https://example.in/bot" in config.effective_user_agent()
