"""SQLite persistence.

Four things live here: discovered creators, their contact points (each with the
URL it came from), an HTTP cache so re-runs do not re-hit anyone's server, and a
suppression list. The suppression list is checked on write *and* on export — an
opt-out has to survive a re-scrape, so it is enforced at both ends.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .models import ContactKind, ContactPoint, Creator, Evidence, Platform, SourceType

#: Cap on a cached page body, so one huge page cannot bloat the database.
MAX_CACHE_CHARS = 400_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
    creator_id            TEXT PRIMARY KEY,
    platform              TEXT NOT NULL,
    native_id             TEXT NOT NULL,
    display_name          TEXT NOT NULL DEFAULT '',
    handle                TEXT NOT NULL DEFAULT '',
    profile_url           TEXT NOT NULL DEFAULT '',
    follower_count        INTEGER,
    follower_count_hidden INTEGER NOT NULL DEFAULT 0,
    country               TEXT NOT NULL DEFAULT '',
    language              TEXT NOT NULL DEFAULT '',
    niche                 TEXT NOT NULL DEFAULT '',
    description           TEXT NOT NULL DEFAULT '',
    links                 TEXT NOT NULL DEFAULT '[]',
    discovered_at         TEXT NOT NULL,
    enriched_at           TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id     TEXT NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    value          TEXT NOT NULL,
    normalized     TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 0,
    is_business    INTEGER NOT NULL DEFAULT 0,
    was_obfuscated INTEGER NOT NULL DEFAULT 0,
    source_url     TEXT NOT NULL DEFAULT '',
    source_type    TEXT NOT NULL DEFAULT '',
    snippet        TEXT NOT NULL DEFAULT '',
    fetched_at     TEXT NOT NULL,
    UNIQUE (creator_id, kind, normalized)
);

CREATE INDEX IF NOT EXISTS idx_contacts_creator ON contacts(creator_id);
CREATE INDEX IF NOT EXISTS idx_creators_followers ON creators(follower_count);
CREATE INDEX IF NOT EXISTS idx_creators_platform ON creators(platform);

CREATE TABLE IF NOT EXISTS suppression (
    normalized TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'email',
    reason     TEXT NOT NULL DEFAULT '',
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
    url          TEXT PRIMARY KEY,
    status       INTEGER NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class Store:
    def __init__(self, path: str | Path = "contacts.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- creators -----------------------------------------------------------

    def upsert_creator(self, creator: Creator) -> None:
        """Insert or update a creator, preserving the original discovery time."""
        self.conn.execute(
            """
            INSERT INTO creators (
                creator_id, platform, native_id, display_name, handle, profile_url,
                follower_count, follower_count_hidden, country, language, niche,
                description, links, discovered_at, enriched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(creator_id) DO UPDATE SET
                display_name          = excluded.display_name,
                handle                = excluded.handle,
                profile_url           = excluded.profile_url,
                follower_count        = excluded.follower_count,
                follower_count_hidden = excluded.follower_count_hidden,
                country               = excluded.country,
                language              = excluded.language,
                niche                 = CASE WHEN excluded.niche != '' THEN excluded.niche
                                             ELSE creators.niche END,
                description           = excluded.description,
                links                 = excluded.links,
                enriched_at           = COALESCE(excluded.enriched_at, creators.enriched_at)
            """,
            (
                creator.creator_id,
                creator.platform.value,
                creator.native_id,
                creator.display_name,
                creator.handle,
                creator.profile_url,
                creator.follower_count,
                int(creator.follower_count_hidden),
                creator.country,
                creator.language,
                creator.niche,
                creator.description[:4000],
                json.dumps(creator.links[:60]),
                creator.discovered_at.isoformat(),
                creator.enriched_at.isoformat() if creator.enriched_at else None,
            ),
        )
        for point in creator.contacts:
            self.add_contact(creator.creator_id, point)
        self.conn.commit()

    def add_contact(self, creator_id: str, point: ContactPoint) -> bool:
        """Store one contact point. Returns False if suppressed or already better."""
        if self.is_suppressed(point.normalized):
            return False
        cur = self.conn.execute(
            """
            INSERT INTO contacts (
                creator_id, kind, value, normalized, confidence, is_business,
                was_obfuscated, source_url, source_type, snippet, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(creator_id, kind, normalized) DO UPDATE SET
                confidence  = MAX(contacts.confidence, excluded.confidence),
                is_business = MAX(contacts.is_business, excluded.is_business),
                source_url  = CASE WHEN excluded.confidence > contacts.confidence
                                   THEN excluded.source_url ELSE contacts.source_url END,
                source_type = CASE WHEN excluded.confidence > contacts.confidence
                                   THEN excluded.source_type ELSE contacts.source_type END,
                snippet     = CASE WHEN excluded.confidence > contacts.confidence
                                   THEN excluded.snippet ELSE contacts.snippet END
            """,
            (
                creator_id,
                point.kind.value,
                point.value,
                point.normalized,
                point.confidence,
                int(point.is_business),
                int(point.was_obfuscated),
                point.evidence.source_url,
                point.evidence.source_type.value,
                point.evidence.snippet[:600],
                point.evidence.fetched_at.isoformat(),
            ),
        )
        return cur.rowcount > 0

    def _row_to_creator(self, row: sqlite3.Row) -> Creator:
        creator = Creator(
            platform=Platform(row["platform"]),
            native_id=row["native_id"],
            display_name=row["display_name"],
            handle=row["handle"],
            profile_url=row["profile_url"],
            follower_count=row["follower_count"],
            follower_count_hidden=bool(row["follower_count_hidden"]),
            country=row["country"],
            language=row["language"],
            niche=row["niche"],
            description=row["description"],
            links=json.loads(row["links"] or "[]"),
        )
        discovered = _parse_dt(row["discovered_at"])
        if discovered:
            creator.discovered_at = discovered
        creator.enriched_at = _parse_dt(row["enriched_at"])
        creator.contacts = self.contacts_for(creator.creator_id)
        return creator

    def contacts_for(self, creator_id: str) -> list[ContactPoint]:
        rows = self.conn.execute(
            "SELECT * FROM contacts WHERE creator_id = ? ORDER BY confidence DESC",
            (creator_id,),
        ).fetchall()
        points: list[ContactPoint] = []
        for row in rows:
            try:
                source_type = SourceType(row["source_type"])
            except ValueError:
                source_type = SourceType.WEBSITE_BODY
            points.append(
                ContactPoint(
                    kind=ContactKind(row["kind"]),
                    value=row["value"],
                    normalized=row["normalized"],
                    confidence=row["confidence"],
                    is_business=bool(row["is_business"]),
                    was_obfuscated=bool(row["was_obfuscated"]),
                    evidence=Evidence(
                        source_url=row["source_url"],
                        source_type=source_type,
                        snippet=row["snippet"],
                        fetched_at=_parse_dt(row["fetched_at"]) or datetime.now(timezone.utc),
                    ),
                )
            )
        return points

    def get_creator(self, creator_id: str) -> Creator | None:
        row = self.conn.execute(
            "SELECT * FROM creators WHERE creator_id = ?", (creator_id,)
        ).fetchone()
        return self._row_to_creator(row) if row else None

    def iter_creators(
        self,
        *,
        platform: Platform | None = None,
        min_followers: int | None = None,
        max_followers: int | None = None,
        needs_enrichment: bool = False,
        limit: int | None = None,
    ):
        clauses: list[str] = []
        params: list[object] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform.value)
        if min_followers is not None:
            clauses.append("(follower_count IS NULL OR follower_count >= ?)")
            params.append(min_followers)
        if max_followers is not None:
            clauses.append("(follower_count IS NULL OR follower_count <= ?)")
            params.append(max_followers)
        if needs_enrichment:
            clauses.append("enriched_at IS NULL")

        sql = "SELECT * FROM creators"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY discovered_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"

        for row in self.conn.execute(sql, params).fetchall():
            yield self._row_to_creator(row)

    def mark_enriched(self, creator_id: str) -> None:
        self.conn.execute(
            "UPDATE creators SET enriched_at = ? WHERE creator_id = ?",
            (_utc_now(), creator_id),
        )
        self.conn.commit()

    def set_links(self, creator_id: str, links: list[str]) -> None:
        self.conn.execute(
            "UPDATE creators SET links = ? WHERE creator_id = ?",
            (json.dumps(links[:60]), creator_id),
        )
        self.conn.commit()

    # -- suppression --------------------------------------------------------

    def suppress(self, normalized: str, *, kind: str = "email", reason: str = "") -> None:
        """Add an opt-out. Also deletes anything already collected for it."""
        key = normalized.strip().lower()
        self.conn.execute(
            """INSERT INTO suppression (normalized, kind, reason, added_at) VALUES (?,?,?,?)
               ON CONFLICT(normalized) DO UPDATE SET reason = excluded.reason""",
            (key, kind, reason, _utc_now()),
        )
        self.conn.execute("DELETE FROM contacts WHERE LOWER(normalized) = ?", (key,))
        self.conn.commit()

    def unsuppress(self, normalized: str) -> None:
        self.conn.execute(
            "DELETE FROM suppression WHERE normalized = ?", (normalized.strip().lower(),)
        )
        self.conn.commit()

    def is_suppressed(self, normalized: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM suppression WHERE normalized = ?", (normalized.strip().lower(),)
        ).fetchone()
        return row is not None

    def suppression_list(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM suppression ORDER BY added_at DESC"
        ).fetchall()

    # -- http cache ---------------------------------------------------------

    def cache_get(self, url: str) -> tuple[int, str, str] | None:
        row = self.conn.execute(
            "SELECT status, body, content_type FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        return (row["status"], row["body"], row["content_type"]) if row else None

    def cache_put(self, url: str, status: int, body: str, content_type: str = "") -> None:
        self.conn.execute(
            """INSERT INTO http_cache (url, status, content_type, body, fetched_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                   status = excluded.status, body = excluded.body,
                   content_type = excluded.content_type, fetched_at = excluded.fetched_at""",
            (url, status, content_type, body[:MAX_CACHE_CHARS], _utc_now()),
        )
        self.conn.commit()

    def clear_cache(self) -> int:
        cur = self.conn.execute("DELETE FROM http_cache")
        self.conn.commit()
        return cur.rowcount

    # -- stats --------------------------------------------------------------

    def stats(self) -> dict[str, object]:
        one = lambda sql, *p: self.conn.execute(sql, p).fetchone()[0]  # noqa: E731
        by_platform = {
            row["platform"]: row["n"]
            for row in self.conn.execute(
                "SELECT platform, COUNT(*) AS n FROM creators GROUP BY platform"
            ).fetchall()
        }
        by_kind = {
            row["kind"]: row["n"]
            for row in self.conn.execute(
                "SELECT kind, COUNT(*) AS n FROM contacts GROUP BY kind"
            ).fetchall()
        }
        return {
            "creators": one("SELECT COUNT(*) FROM creators"),
            "creators_by_platform": by_platform,
            "contacts": one("SELECT COUNT(*) FROM contacts"),
            "contacts_by_kind": by_kind,
            "with_email": one(
                "SELECT COUNT(DISTINCT creator_id) FROM contacts WHERE kind = 'email'"
            ),
            "business_emails": one(
                "SELECT COUNT(*) FROM contacts WHERE kind = 'email' AND is_business = 1"
            ),
            "unenriched": one("SELECT COUNT(*) FROM creators WHERE enriched_at IS NULL"),
            "suppressed": one("SELECT COUNT(*) FROM suppression"),
            "cached_pages": one("SELECT COUNT(*) FROM http_cache"),
        }
