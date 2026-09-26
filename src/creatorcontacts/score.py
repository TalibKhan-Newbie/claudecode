"""Decide which creators are actually worth exporting.

Your rule was "any 2 of name / number / address / email". Implemented literally
that is almost free — a name comes with every record, so name + anything passes.
The gate here therefore has two knobs:

``min_fields``
    How many distinct contact kinds must be present. Your rule is 2.

``require_reachable``
    Whether one of them has to be something you can actually send a message to
    (an email or a phone). On by default, because a row of name + Instagram URL
    is not a lead you can email.

Turn ``require_reachable`` off if you want the literal rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ContactKind, Creator

#: Kinds you can actually initiate contact through.
REACHABLE_KINDS = frozenset({ContactKind.EMAIL, ContactKind.PHONE})

#: Counts toward ``min_fields``. NAME is excluded — see ``resolve_name``.
COUNTABLE_KINDS = frozenset(
    {ContactKind.EMAIL, ContactKind.PHONE, ContactKind.ADDRESS, ContactKind.SOCIAL}
)


@dataclass
class GateConfig:
    min_fields: int = 2
    require_reachable: bool = True
    min_confidence: float = 0.5
    #: Require the email to look deliberately published for business contact.
    require_business_email: bool = False
    #: Count the display name as one of the fields (your literal rule).
    count_name_as_field: bool = True


@dataclass
class GateResult:
    passed: bool
    field_count: int
    kinds: tuple[str, ...]
    reachable: bool
    reason: str = ""

    @property
    def summary(self) -> str:
        return f"{self.field_count} fields ({', '.join(self.kinds) or 'none'})"


def resolve_name(creator: Creator) -> str:
    """The creator's public/professional name, never a derived legal identity.

    This is the display name or handle they chose to publish, and nothing else.
    No attempt is made to resolve it to a real or legal name.
    """
    explicit = creator.best(ContactKind.NAME)
    if explicit and explicit.value.strip():
        return explicit.value.strip()
    return (creator.display_name or creator.handle or "").strip()


def evaluate(creator: Creator, config: GateConfig | None = None) -> GateResult:
    """Apply the field gate to one creator."""
    config = config or GateConfig()

    usable = [
        point
        for point in creator.contacts
        if point.confidence >= config.min_confidence and point.kind in COUNTABLE_KINDS
    ]

    if config.require_business_email:
        emails = [p for p in usable if p.kind is ContactKind.EMAIL]
        if emails and not any(p.is_business for p in emails):
            usable = [p for p in usable if p.kind is not ContactKind.EMAIL]

    kinds = {point.kind for point in usable}
    field_count = len(kinds)

    has_name = bool(resolve_name(creator))
    if config.count_name_as_field and has_name:
        field_count += 1

    reachable = bool(kinds & REACHABLE_KINDS)

    if config.require_reachable and not reachable:
        return GateResult(
            passed=False,
            field_count=field_count,
            kinds=tuple(sorted(k.value for k in kinds)),
            reachable=False,
            reason="no email or phone — cannot be contacted",
        )

    if field_count < config.min_fields:
        return GateResult(
            passed=False,
            field_count=field_count,
            kinds=tuple(sorted(k.value for k in kinds)),
            reachable=reachable,
            reason=f"only {field_count} field(s), need {config.min_fields}",
        )

    return GateResult(
        passed=True,
        field_count=field_count,
        kinds=tuple(sorted(k.value for k in kinds)),
        reachable=reachable,
    )


def lead_score(creator: Creator) -> float:
    """0-100 ranking for outreach order. Higher = better lead.

    Weighted toward a business email from a deliberate source, since that is what
    actually gets a reply.
    """
    score = 0.0

    email = creator.best(ContactKind.EMAIL)
    if email:
        score += 45 * email.confidence
        if email.is_business:
            score += 15
        if email.was_obfuscated:
            score -= 10

    if creator.best(ContactKind.PHONE):
        score += 12
    if creator.best(ContactKind.ADDRESS):
        score += 6
    score += min(len(creator.contacts_of(ContactKind.SOCIAL)) * 3, 9)

    if creator.links:
        score += 5
    if creator.follower_count:
        score += 8

    return round(min(score, 100.0), 1)


def partition(
    creators: list[Creator], config: GateConfig | None = None
) -> tuple[list[tuple[Creator, GateResult]], list[tuple[Creator, GateResult]]]:
    """Split into (exportable, rejected), each ranked by lead score."""
    config = config or GateConfig()
    passed: list[tuple[Creator, GateResult]] = []
    failed: list[tuple[Creator, GateResult]] = []

    for creator in creators:
        result = evaluate(creator, config)
        (passed if result.passed else failed).append((creator, result))

    passed.sort(key=lambda pair: -lead_score(pair[0]))
    failed.sort(key=lambda pair: -pair[1].field_count)
    return passed, failed
