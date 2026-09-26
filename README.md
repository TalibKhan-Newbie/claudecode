# creator-contacts

Find the **published business-contact details** of mid-size creators — the
50k–60k tier who still read their own email but already have real reach.

Built for influencer outreach: you give it a niche, it gives you a ranked CSV of
creators with at least two contact fields and a source URL for every value.

```
discover                    enrich                      export
─────────                   ──────                      ──────
YouTube Data API      →     follow their links     →    ranked CSV/XLSX
podcast RSS feeds     →     linktree → site        →    ≥2 fields each
licensed IG vendors   →     → /contact page        →    source URL per value
      ↓                            ↓                          ↓
  50k–60k band            mailto:, tel:, schema.org     suppression applied
```

## Why these sources

| Source | Cost | How it works |
|---|---|---|
| **YouTube** | Free | Official Data API v3. Real subscriber counts, so the 50k–60k filter is exact. |
| **Podcasts** | Free | RSS requires `<itunes:email>` for directory submission — every podcaster has published a working contact address on purpose. |
| **Their own website** | Free | Follow the links they published: Linktree → site → `/contact`. `mailto:`/`tel:` links and schema.org markup are the strongest signals. |
| **Instagram / TikTok** | Paid | No compliant public search API exists. Needs a licensed vendor — see [`providers.py`](src/creatorcontacts/sources/providers.py). |

YouTube and podcasts carry the project and cost nothing. Instagram is the one
that needs a budget, and the reason why is documented rather than worked around.

## Try it with zero setup

No API key, no `pip install` — pure standard library:

```bash
python3 scripts/podcast_contacts.py --term "hindi podcast" --country IN
```

It searches Apple Podcasts (free, keyless), reads each show's RSS feed, and
writes a CSV of published owner/business emails. Use it to see real output before
setting anything up. `--term "standup comedy"`, `--term "hindi business"`, etc.

The full package below adds YouTube, link following, a database and suppression.

## Install

PyPI access is required for the dependencies:

```bash
git clone https://github.com/TalibKhan-Newbie/claudecode.git
cd claudecode
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,xlsx]"
cp .env.example .env     # then add your YouTube API key
```

Get a free YouTube Data API key at
[console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials),
then enable **YouTube Data API v3**.

## Use

```bash
# 1. Find channels in the band (comedy niche, Shorts creators, India)
creator-contacts discover youtube --niche comedy --shorts

# 2. Podcasts — owner email straight from the RSS feed
creator-contacts discover podcasts --niche podcast

# 3. Follow everyone's links to find contact pages
creator-contacts enrich --limit 100

# 4. Export the ranked list
creator-contacts export leads.csv

# Check what you have
creator-contacts stats
creator-contacts show "rahul"          # full provenance for one creator
```

### The "any 2 fields" rule

Your rule was *at least 2 of name / phone / address / email*. Two knobs control it:

```yaml
gate:
  min_fields: 2            # how many distinct fields required
  require_reachable: true  # ...at least one must be email or phone
```

`require_reachable` is on because a row of *name + Instagram URL* is not a lead
you can email. For the literal reading:

```bash
creator-contacts export leads.csv --allow-unreachable
```

Only want addresses that are clearly published for business?

```bash
creator-contacts export leads.csv --business-email-only
```

### Finding the small channels

Generic search terms return the giants. Specific ones find your tier:

```yaml
niches:
  comedy:
    - delhi open mic comedy      # good — finds 50k channels
    - mumbai standup crowd work  # good
    # - comedy                   # bad — returns the millionaires
```

`discover youtube` searches **videos**, not channels, because video search
surfaces small channels that channel-search buries. Add `--published-after
2025-01-01T00:00:00Z` to skip dormant accounts.

### Watch your quota

The Data API gives 10,000 units/day. A search costs **100**, hydrating 50
channels costs **1**. So one search checks 50 channels for 101 units — budget
roughly 90 searches a day. The CLI tracks it and stops cleanly:

```bash
creator-contacts discover youtube --niche comedy --quota 5000 --pages 1
```

## What it will not do

Short version: **business contact info yes, personal identity no.**

Phone numbers and postal addresses are only ever read from business-designated
sources — a `tel:` link, schema.org markup, or a labelled contact page. Free prose
is scanned for emails and nothing else. People-search and data-broker sites
(Truecaller, Whitepages, Spokeo…) are blocked at the network layer, and
`tests/test_links.py` fails the build if that list is trimmed.

There is no CAPTCHA solving. YouTube gates the About-page email behind a human
check; that check is the answer, so the channel description and the creator's own
links are used instead.

Full reasoning, plus the GDPR/DPDP notes: **[`docs/SCOPE.md`](docs/SCOPE.md)**.

## Being a good citizen

Defaults that keep you off blocklists, all in `config.yaml`:

- **robots.txt obeyed** on every fetch, `Crawl-delay` included
- **1.5s between requests per host**, one host at a time
- **Identifying User-Agent** with a contact URL — set `OUTREACH_CONTACT_URL`
- **Response caching**, so re-runs don't re-hit anyone's server
- **Obfuscated emails skipped** by default — `name [at] site.com` is a signal, honour it
- **Suppression enforced on write and on export**, so an opt-out survives a re-scrape

```bash
creator-contacts suppress add someone@example.com --reason "asked to be removed"
```

## Layout

```
src/creatorcontacts/
├── models.py       # Creator, ContactPoint, Evidence — provenance lives here
├── extract.py      # email/phone/address extraction + the business-source rule
├── net.py          # robots.txt, per-host throttle, caching, size caps
├── store.py        # SQLite: creators, contacts, suppression, HTTP cache
├── score.py        # the ≥2-field gate and lead ranking
├── export.py       # CSV / XLSX with source columns
├── config.py       # config.yaml + env
├── cli.py          # typer CLI
└── sources/
    ├── youtube.py   # Data API v3 + quota budget
    ├── podcast.py   # iTunes + Podcast Index + RSS owner email
    ├── links.py     # link-hub expansion, contact-page crawl, SKIP_HOSTS
    └── providers.py # Modash / Phyllo adapters for IG + TikTok
```

## Tests

```bash
pytest              # ~120 tests, no network — every input is a literal
ruff check .
```

## One honest caveat

Cold outreach converts in the low single digits even done well. 200 creators you
have actually watched, with a first line proving it, beats 20,000 rows. This tool
exists to make finding the 200 fast.

## License

MIT
