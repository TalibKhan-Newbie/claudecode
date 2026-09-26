"""Config loading: ``config.yaml`` merged over defaults, env vars for secrets."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.yaml"


@dataclass
class CrawlConfig:
    delay_seconds: float = 1.5
    timeout_seconds: float = 15.0
    max_retries: int = 2
    obey_robots: bool = True
    max_pages_per_creator: int = 8
    user_agent: str = ""
    #: Skip emails the creator wrote as ``name [at] site.com``. See extract.py.
    respect_obfuscation: bool = True


@dataclass
class BandConfig:
    min_followers: int = 50_000
    max_followers: int = 60_000
    include_hidden_counts: bool = False


@dataclass
class GateSettings:
    min_fields: int = 2
    require_reachable: bool = True
    min_confidence: float = 0.5
    require_business_email: bool = False
    count_name_as_field: bool = True


@dataclass
class AppConfig:
    database: str = "contacts.db"
    region: str = "IN"
    language: str | None = "hi"
    band: BandConfig = field(default_factory=BandConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    gate: GateSettings = field(default_factory=GateSettings)
    #: niche name -> search terms
    niches: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> AppConfig:
        config = cls()
        target = Path(path or DEFAULT_CONFIG_PATH)
        if not target.exists():
            log.debug("no config at %s, using defaults", target)
            return config

        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}

        for key in ("database", "region", "language"):
            if key in raw:
                setattr(config, key, raw[key])

        for section, holder in (
            ("band", config.band),
            ("crawl", config.crawl),
            ("gate", config.gate),
        ):
            for key, value in (raw.get(section) or {}).items():
                if hasattr(holder, key):
                    setattr(holder, key, value)
                else:
                    log.warning("unknown %s option in config: %s", section, key)

        niches = raw.get("niches") or {}
        config.niches = {
            str(name): [str(term) for term in (terms or [])] for name, terms in niches.items()
        }
        return config

    def terms_for(self, niche: str) -> list[str]:
        """Search terms for ``niche``, falling back to the niche name itself."""
        return self.niches.get(niche) or [niche]

    def all_niches(self) -> list[str]:
        return sorted(self.niches)

    @property
    def contact_url(self) -> str:
        """Set ``OUTREACH_CONTACT_URL`` so site owners can reach you. Good practice."""
        return os.getenv("OUTREACH_CONTACT_URL", "")

    def effective_user_agent(self) -> str:
        if self.crawl.user_agent:
            return self.crawl.user_agent
        contact = self.contact_url or "set OUTREACH_CONTACT_URL"
        return (
            f"creator-contacts/0.1 (+{contact}; business-outreach research; "
            "respects robots.txt)"
        )
