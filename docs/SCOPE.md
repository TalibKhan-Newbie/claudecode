# Scope: what this tool collects, and what it refuses to

This file is the contract the code enforces. Several modules point here in their
comments. If you change the scope, change it here first.

## What it collects

Contact details that creators **publish so brands can reach them**:

| Value | Where it comes from |
|---|---|
| Public/professional name | Channel title, show title, handle |
| Business email | Channel description, `mailto:` link, contact page, `<itunes:email>` in a podcast RSS feed, schema.org `email` |
| Business phone | `tel:` link, schema.org `telephone`, a labelled contact page |
| Business address | schema.org `PostalAddress`, a line labelled `Address:` / `Registered office:` on a contact page |
| Social profiles | Links the creator published on their own profile |
| Audience size | YouTube Data API `statistics.subscriberCount` |

A creator who writes "Business enquiries: me@example.com" in their channel
description has asked to be contacted there. That is the entire target set.

## What it refuses to collect

**Residential addresses and personal phone numbers.** A creator's home address is
not business contact info, and a tool that assembles one is a doxxing tool
whatever it is used for. This is enforced in three places, not left to
discipline:

1. `models.BUSINESS_ONLY_SOURCES` — phone and address values are only accepted
   from a `tel:` link, structured business markup, a labelled contact page, or a
   podcast RSS owner block.
2. `extract.extract_phones` / `extract.extract_labelled_address` — both return
   `[]` immediately for any other source type. Free prose is scanned for emails
   and nothing else. An address additionally requires an explicit label; the code
   never guesses that a blob of text is an address.
3. `sources/links.SKIP_HOSTS` — people-search and data-broker domains
   (Truecaller, Whitepages, Spokeo, BeenVerified, FastPeopleSearch, Radaris and
   the rest) are blocked at the network layer. `tests/test_links.py` asserts
   this, so removing one breaks the build.

**Anything behind a human check.** YouTube puts the About-page business email
behind a CAPTCHA. There is no CAPTCHA solving and no headless-browser fallback
here. Where a platform has put up a human check, that check is the answer. What
the tool uses instead is the channel description and the creator's own published
links.

**Platforms whose terms forbid automated access.** Instagram, Facebook, LinkedIn,
TikTok and X are in `SKIP_HOSTS`. For Instagram and TikTok discovery the
supported route is a licensed vendor — see `sources/providers.py`.

**Obfuscated emails, by default.** A creator who writes `name [at] gmail [dot] com`
is signalling they do not want to be auto-harvested. `crawl.respect_obfuscation`
is `true` by default and skips those. You can turn it off; the setting exists so
that turning it off is a deliberate act.

## Legal notes

Not legal advice — talk to a lawyer before running this at scale.

**Public data is not free data.** *hiQ v. LinkedIn* (9th Cir. 2022) held that
scraping public pages is not "unauthorised access" under the CFAA. It did **not**
void the terms of service you accepted, and Meta has sued scrapers on precisely
that contract basis. Terms still bind you.

**A business email is still personal data.** Under GDPR and India's DPDP Act
2023, `rahul@studiokaam.in` identifies a person. Practical consequences:

- **Say where you got it.** GDPR Art. 14 requires disclosing the source of
  personal data not collected from the person directly. This is why every
  exported row carries `email_source_url` — keep those columns.
- **Have a lawful basis.** For B2B outreach to a published business address,
  legitimate interest is the usual one, and it requires the message be relevant
  to their business. A blast to 5,000 creators is not.
- **Honour opt-outs immediately.** `creator-contacts suppress add <email>` deletes
  what was collected and blocks re-collection on later runs. The suppression
  check runs on write *and* on export, so an opt-out survives a re-scrape.
- **Include an unsubscribe route** in every message. CAN-SPAM requires it in the
  US; it is good practice everywhere.

**Consent under DPDP.** India's DPDP Act is consent-first and its exemptions for
publicly available data are narrower than GDPR's. If you are targeting Indian
creators from India, get advice on your basis before a large campaign.

## The honest limitation

Even done properly, cold outreach converts in the low single digits. A list of
200 creators you have actually watched, with a first line proving it, beats
20,000 rows every time. This tool is built to make the 200 easy to find — not to
make the 20,000 feel productive.
