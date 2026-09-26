"""Export to CSV or XLSX.

Every exported row carries the source URL for each contact value. Keep those
columns — they are how you answer "where did you get this?", and under GDPR
Article 14 (or India's DPDP Act) you are expected to be able to state the source
of personal data you did not collect from the person directly.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from .models import ContactKind, Creator
from .score import (
    GateConfig,
    GateResult,
    best_email,
    email_domain,
    lead_score,
    partition,
    resolve_name,
)

log = logging.getLogger(__name__)

COLUMNS = [
    "creator_id",
    "platform",
    "name",
    "handle",
    "profile_url",
    "followers",
    "followers_hidden",
    "country",
    "language",
    "niche",
    "lead_score",
    "field_count",
    "fields_present",
    "email",
    "email_domain",
    "email_is_business",
    "email_confidence",
    "email_source_url",
    "email_source_type",
    "phone",
    "phone_source_url",
    "address",
    "address_source_url",
    "socials",
    "website",
    "discovered_at",
    "enriched_at",
]


def row_for(
    creator: Creator, gate: GateResult, config: GateConfig | None = None
) -> dict[str, object]:
    # best_email respects allowed_email_domains, so a Gmail-only run cannot leak
    # the custom-domain address the creator also published.
    email = best_email(creator, config)
    phone = creator.best(ContactKind.PHONE)
    address = creator.best(ContactKind.ADDRESS)
    socials = creator.contacts_of(ContactKind.SOCIAL)

    website = next(
        (
            link
            for link in creator.links
            if link.startswith("http")
            and not any(
                host in link
                for host in ("youtube.com", "instagram.com", "twitter.com", "x.com", "facebook.com")
            )
        ),
        "",
    )

    return {
        "creator_id": creator.creator_id,
        "platform": creator.platform.value,
        "name": resolve_name(creator),
        "handle": creator.handle,
        "profile_url": creator.profile_url,
        "followers": creator.follower_count if creator.follower_count is not None else "",
        "followers_hidden": "yes" if creator.follower_count_hidden else "",
        "country": creator.country,
        "language": creator.language,
        "niche": creator.niche,
        "lead_score": lead_score(creator),
        "field_count": gate.field_count,
        "fields_present": "|".join(gate.kinds),
        "email": email.normalized if email else "",
        "email_domain": email_domain(email.normalized) if email else "",
        "email_is_business": "yes" if (email and email.is_business) else "",
        "email_confidence": email.confidence if email else "",
        "email_source_url": email.evidence.source_url if email else "",
        "email_source_type": email.evidence.source_type.value if email else "",
        "phone": phone.normalized if phone else "",
        "phone_source_url": phone.evidence.source_url if phone else "",
        "address": address.normalized if address else "",
        "address_source_url": address.evidence.source_url if address else "",
        "socials": " | ".join(s.value for s in socials),
        "website": website,
        "discovered_at": creator.discovered_at.isoformat(timespec="seconds"),
        "enriched_at": creator.enriched_at.isoformat(timespec="seconds") if creator.enriched_at else "",
    }


def to_csv(
    creators: list[Creator],
    out_path: str | Path,
    *,
    config: GateConfig | None = None,
    include_rejected: bool = False,
    target: int | None = None,
) -> tuple[int, int]:
    """Write passing creators to ``out_path``. Returns (written, rejected).

    ``target`` caps the row count, keeping the highest-scoring leads.
    """
    passed, failed = partition(creators, config, target=target)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for creator, gate in passed:
            writer.writerow(row_for(creator, gate, config))

    if include_rejected and failed:
        reject_path = out.with_name(out.stem + "_rejected" + out.suffix)
        with reject_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=COLUMNS + ["reject_reason"], extrasaction="ignore"
            )
            writer.writeheader()
            for creator, gate in failed:
                writer.writerow({**row_for(creator, gate, config), "reject_reason": gate.reason})
        log.info("wrote %d rejected rows to %s", len(failed), reject_path)

    log.info("wrote %d rows to %s (%d rejected)", len(passed), out, len(failed))
    return len(passed), len(failed)


def to_xlsx(
    creators: list[Creator],
    out_path: str | Path,
    *,
    config: GateConfig | None = None,
    target: int | None = None,
) -> tuple[int, int]:
    """Same as :func:`to_csv` but formatted, with clickable source links."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise RuntimeError("XLSX export needs openpyxl: pip install openpyxl") from exc

    passed, failed = partition(creators, config, target=target)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Leads"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")
    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for creator, gate in passed:
        row = row_for(creator, gate, config)
        sheet.append([row.get(column, "") for column in COLUMNS])

    for index, column in enumerate(COLUMNS, start=1):
        width = {"email_source_url": 46, "profile_url": 40, "address": 40, "socials": 44}.get(column, 20)
        sheet.column_dimensions[get_column_letter(index)].width = width

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(out)
    log.info("wrote %d rows to %s (%d rejected)", len(passed), out, len(failed))
    return len(passed), len(failed)
