"""Command line interface.

    creator-contacts discover youtube --niche comedy --shorts
    creator-contacts discover podcasts --niche comedy
    creator-contacts discover providers --platform instagram --niche comedy
    creator-contacts enrich --limit 100
    creator-contacts export leads.csv
    creator-contacts stats
    creator-contacts suppress add someone@example.com --reason "asked to be removed"
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .config import AppConfig
from .export import to_csv, to_xlsx
from .models import Platform
from .net import Fetcher
from .score import GateConfig, evaluate, lead_score, partition, resolve_name
from .sources.links import LinkEnricher
from .sources.podcast import PodcastSource, link_creator_by_name
from .sources.providers import available_providers
from .sources.youtube import QuotaBudget, QuotaExceeded, YouTubeSource
from .store import Store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find published business-contact details for mid-size creators.",
)
discover_app = typer.Typer(no_args_is_help=True, help="Find creators in the follower band.")
suppress_app = typer.Typer(no_args_is_help=True, help="Manage the do-not-contact list.")
app.add_typer(discover_app, name="discover")
app.add_typer(suppress_app, name="suppress")

console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _build(config_path: Optional[str], verbose: bool) -> tuple[AppConfig, Store, Fetcher]:
    _setup_logging(verbose)
    config = AppConfig.load(config_path)
    store = Store(config.database)
    fetcher = Fetcher(
        user_agent=config.effective_user_agent(),
        delay=config.crawl.delay_seconds,
        timeout=config.crawl.timeout_seconds,
        max_retries=config.crawl.max_retries,
        obey_robots=config.crawl.obey_robots,
        cache_get=store.cache_get,
        cache_put=store.cache_put,
    )
    return config, store, fetcher


def _gate_from(config: AppConfig) -> GateConfig:
    return GateConfig(
        min_fields=config.gate.min_fields,
        require_reachable=config.gate.require_reachable,
        min_confidence=config.gate.min_confidence,
        require_business_email=config.gate.require_business_email,
        count_name_as_field=config.gate.count_name_as_field,
    )


def _report(label: str, creators: list, store: Store, gate: GateConfig) -> None:
    saved = 0
    for creator in creators:
        store.upsert_creator(creator)
        saved += 1

    passed, failed = partition(creators, gate)
    console.print(
        f"\n[bold green]{label}[/]: saved {saved} creators — "
        f"[green]{len(passed)} already meet the {gate.min_fields}-field bar[/], "
        f"[yellow]{len(failed)} need enrichment[/]"
    )
    if failed:
        console.print("[dim]Run `creator-contacts enrich` to follow their links.[/]")


# -- discover ---------------------------------------------------------------


@discover_app.command("youtube")
def discover_youtube(
    niche: str = typer.Option(..., "--niche", "-n", help="Niche key from config.yaml."),
    queries: Optional[list[str]] = typer.Option(None, "--query", "-q", help="Override search terms."),
    shorts: bool = typer.Option(False, "--shorts", help="Bias toward Shorts creators."),
    pages: int = typer.Option(2, "--pages", help="Search pages per term (100 quota units each)."),
    region: Optional[str] = typer.Option(None, "--region"),
    language: Optional[str] = typer.Option(None, "--language"),
    min_followers: Optional[int] = typer.Option(None, "--min-subs"),
    max_followers: Optional[int] = typer.Option(None, "--max-subs"),
    quota: int = typer.Option(10_000, "--quota", help="Daily Data API unit budget."),
    published_after: Optional[str] = typer.Option(
        None, "--published-after", help="RFC3339, e.g. 2025-01-01T00:00:00Z — finds active channels."
    ),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Find YouTube channels inside the subscriber band via the Data API."""
    config, store, fetcher = _build(config_path, verbose)
    terms = list(queries) if queries else config.terms_for(niche)
    band = config.band

    try:
        source = YouTubeSource(fetcher, budget=QuotaBudget(limit=quota))
    except ValueError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(code=2)

    console.print(f"[bold]Searching YouTube[/] for {len(terms)} term(s) in niche [cyan]{niche}[/]")

    try:
        creators = source.discover(
            terms,
            min_followers=min_followers if min_followers is not None else band.min_followers,
            max_followers=max_followers if max_followers is not None else band.max_followers,
            region=region or config.region,
            language=language if language is not None else config.language,
            shorts_only=shorts,
            published_after=published_after,
            pages=pages,
            niche=niche,
            include_hidden_counts=band.include_hidden_counts,
        )
    except QuotaExceeded as exc:
        console.print(f"[bold red]Quota exhausted:[/] {exc}")
        raise typer.Exit(code=1)
    finally:
        console.print(f"[dim]Quota used: {source.budget.summary()}[/]")

    _report("YouTube", creators, store, _gate_from(config))
    fetcher.close()
    store.close()


@discover_app.command("podcasts")
def discover_podcasts(
    niche: str = typer.Option(..., "--niche", "-n"),
    terms: Optional[list[str]] = typer.Option(None, "--term", "-q"),
    country: Optional[str] = typer.Option(None, "--country"),
    limit: int = typer.Option(50, "--limit", help="Shows per search term."),
    match_band: bool = typer.Option(
        False,
        "--match-band",
        help="Only keep shows matching an already-discovered in-band channel.",
    ),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Find podcasts and read the owner email from their RSS feed."""
    config, store, fetcher = _build(config_path, verbose)
    search_terms = list(terms) if terms else config.terms_for(niche)
    source = PodcastSource(fetcher)

    console.print(f"[bold]Searching podcast directories[/] for niche [cyan]{niche}[/]")
    creators = source.discover(
        search_terms,
        country=country or config.region,
        limit_per_term=limit,
        niche=niche,
    )

    if match_band:
        known = list(store.iter_creators())
        matched = []
        for creator in creators:
            partner = link_creator_by_name(creator, known)
            if partner is not None:
                creator.follower_count = creator.follower_count or partner.follower_count
                matched.append(creator)
        console.print(
            f"[dim]--match-band: {len(matched)}/{len(creators)} shows matched a known channel[/]"
        )
        creators = matched

    _report("Podcasts", creators, store, _gate_from(config))
    fetcher.close()
    store.close()


@discover_app.command("providers")
def discover_providers(
    platform: str = typer.Option("instagram", "--platform", "-p"),
    niche: str = typer.Option("", "--niche", "-n"),
    country: Optional[str] = typer.Option(None, "--country"),
    limit: int = typer.Option(100, "--limit"),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Query licensed influencer-data vendors (the compliant route for Instagram)."""
    config, store, fetcher = _build(config_path, verbose)

    try:
        target = Platform(platform.lower())
    except ValueError:
        console.print(f"[bold red]Unknown platform:[/] {platform}")
        raise typer.Exit(code=2)

    providers = available_providers(fetcher)
    if not providers:
        console.print(
            "[yellow]No provider API keys configured.[/]\n"
            "Instagram and TikTok have no compliant public search API — see "
            "[cyan]src/creatorcontacts/sources/providers.py[/] for the vendor list "
            "and set MODASH_API_KEY or INSIGHTIQ_CLIENT_ID/INSIGHTIQ_SECRET.\n"
            "[dim]YouTube and podcasts need no vendor and work right now.[/]"
        )
        raise typer.Exit(code=1)

    collected = []
    for provider in providers:
        console.print(f"[bold]Querying {provider.name}[/]")
        collected.extend(
            provider.search(
                platform=target,
                min_followers=config.band.min_followers,
                max_followers=config.band.max_followers,
                niche=niche,
                country=country or config.region,
                limit=limit,
            )
        )

    _report("Providers", collected, store, _gate_from(config))
    fetcher.close()
    store.close()


# -- enrich -----------------------------------------------------------------


@app.command()
def enrich(
    limit: int = typer.Option(50, "--limit", "-l", help="Creators to process."),
    platform: Optional[str] = typer.Option(None, "--platform", "-p"),
    all_creators: bool = typer.Option(False, "--all", help="Re-enrich already-enriched creators."),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Follow each creator's published links and pull contacts off their pages."""
    config, store, fetcher = _build(config_path, verbose)

    target_platform = Platform(platform.lower()) if platform else None
    pending = list(
        store.iter_creators(
            platform=target_platform,
            needs_enrichment=not all_creators,
            limit=limit,
        )
    )
    if not pending:
        console.print("[yellow]Nothing to enrich.[/] Run a `discover` command first.")
        raise typer.Exit()

    enricher = LinkEnricher(
        fetcher,
        respect_obfuscation=config.crawl.respect_obfuscation,
        max_pages_per_creator=config.crawl.max_pages_per_creator,
        region=config.region,
    )
    gate = _gate_from(config)
    total_added = 0
    now_passing = 0

    with console.status("[bold]Enriching…[/]") as status:
        for index, creator in enumerate(pending, start=1):
            status.update(
                f"[bold]Enriching[/] {index}/{len(pending)}: {creator.display_name[:40]}"
            )
            added = enricher.enrich(creator)
            total_added += added
            store.upsert_creator(creator)
            store.mark_enriched(creator.creator_id)
            if evaluate(creator, gate).passed:
                now_passing += 1

    console.print(
        f"\n[bold green]Enriched {len(pending)} creators[/]: "
        f"+{total_added} contact points, [green]{now_passing} now meet the bar[/]"
    )
    fetcher.close()
    store.close()


# -- export -----------------------------------------------------------------


@app.command("export")
def export_cmd(
    out: Path = typer.Argument(Path("leads.csv"), help="Output .csv or .xlsx path."),
    platform: Optional[str] = typer.Option(None, "--platform", "-p"),
    min_fields: Optional[int] = typer.Option(None, "--min-fields"),
    allow_unreachable: bool = typer.Option(
        False,
        "--allow-unreachable",
        help="Export rows with no email or phone (your literal 'any 2 fields' rule).",
    ),
    business_email_only: bool = typer.Option(False, "--business-email-only"),
    include_rejected: bool = typer.Option(False, "--include-rejected"),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Write the gated, ranked lead list to CSV or XLSX."""
    config, store, fetcher = _build(config_path, verbose)
    fetcher.close()

    gate = _gate_from(config)
    if min_fields is not None:
        gate.min_fields = min_fields
    if allow_unreachable:
        gate.require_reachable = False
    if business_email_only:
        gate.require_business_email = True

    creators = list(store.iter_creators(platform=Platform(platform.lower()) if platform else None))
    if not creators:
        console.print("[yellow]Database is empty.[/] Run a `discover` command first.")
        raise typer.Exit()

    if out.suffix.lower() in (".xlsx", ".xlsm"):
        written, rejected = to_xlsx(creators, out, config=gate)
    else:
        written, rejected = to_csv(creators, out, config=gate, include_rejected=include_rejected)

    console.print(
        f"[bold green]Wrote {written} leads[/] to [cyan]{out}[/] "
        f"([yellow]{rejected} below the {gate.min_fields}-field bar[/])"
    )
    console.print(
        "[dim]Source-URL columns are included on purpose — keep them so you can "
        "always show where a value came from.[/]"
    )
    store.close()


# -- suppression ------------------------------------------------------------


@suppress_app.command("add")
def suppress_add(
    value: str = typer.Argument(..., help="Email or E.164 phone to suppress."),
    reason: str = typer.Option("", "--reason", "-r"),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Add an opt-out and delete anything already collected for it."""
    config = AppConfig.load(config_path)
    with Store(config.database) as store:
        store.suppress(value, reason=reason)
    console.print(f"[green]Suppressed[/] {value} — existing records deleted.")


@suppress_app.command("remove")
def suppress_remove(
    value: str = typer.Argument(...),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Remove an entry from the do-not-contact list."""
    config = AppConfig.load(config_path)
    with Store(config.database) as store:
        store.unsuppress(value)
    console.print(f"[green]Removed[/] {value} from the suppression list.")


@suppress_app.command("list")
def suppress_list(
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Show the do-not-contact list."""
    config = AppConfig.load(config_path)
    with Store(config.database) as store:
        rows = store.suppression_list()

    if not rows:
        console.print("[dim]Suppression list is empty.[/]")
        return

    table = Table(title="Do-not-contact")
    table.add_column("Value", style="cyan")
    table.add_column("Kind")
    table.add_column("Reason")
    table.add_column("Added")
    for row in rows:
        table.add_row(row["normalized"], row["kind"], row["reason"] or "—", row["added_at"][:10])
    console.print(table)


# -- misc -------------------------------------------------------------------


@app.command()
def stats(
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Summarise what is in the database."""
    config = AppConfig.load(config_path)
    with Store(config.database) as store:
        data = store.stats()

    table = Table(title=f"creator-contacts — {config.database}")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right", style="cyan")
    for key, value in data.items():
        if isinstance(value, dict):
            value = ", ".join(f"{k}={v}" for k, v in sorted(value.items())) or "—"
        table.add_row(key.replace("_", " "), str(value))
    console.print(table)


@app.command("show")
def show(
    query: str = typer.Argument(..., help="Substring of a name, handle, or creator_id."),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Inspect one creator's contacts with full provenance."""
    config = AppConfig.load(config_path)
    needle = query.lower()

    with Store(config.database) as store:
        matches = [
            creator
            for creator in store.iter_creators()
            if needle in creator.display_name.lower()
            or needle in creator.handle.lower()
            or needle in creator.creator_id.lower()
        ]

        if not matches:
            console.print(f"[yellow]No creator matching[/] {query!r}")
            raise typer.Exit(code=1)

        for creator in matches[:5]:
            gate = evaluate(creator, _gate_from(config))

            audience = f", {creator.follower_count:,} followers" if creator.follower_count else ""
            console.print(
                f"\n[bold cyan]{resolve_name(creator)}[/] ({creator.platform.value}{audience})"
            )
            console.print(f"  {creator.profile_url}")

            verdict = "[green]PASS[/]" if gate.passed else f"[red]FAIL[/] ({gate.reason})"
            console.print(
                f"  gate: {verdict} — {gate.summary}, lead score {lead_score(creator)}"
            )

            if not creator.contacts:
                console.print("  [dim]no contacts found yet — try `enrich`[/]")
                continue

            table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
            table.add_column("kind")
            table.add_column("value", style="cyan")
            table.add_column("conf", justify="right")
            table.add_column("biz")
            table.add_column("source")
            for point in sorted(creator.contacts, key=lambda p: -p.confidence):
                table.add_row(
                    point.kind.value,
                    point.normalized[:60],
                    f"{point.confidence:.2f}",
                    "yes" if point.is_business else "",
                    f"{point.evidence.source_type.value} · {point.evidence.source_url[:50]}",
                )
            console.print(table)


@app.command()
def clear_cache(
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Empty the HTTP response cache."""
    config = AppConfig.load(config_path)
    with Store(config.database) as store:
        removed = store.clear_cache()
    console.print(f"[green]Cleared[/] {removed} cached pages.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
