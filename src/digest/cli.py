"""`digest` command line entry point.

Subcommands are separate from the start so ranking can be iterated on without re-fetching
(PLAN.md section 7, phase 0). `fetch` and `render` are live as of phase 2; `rank` and `run`
remain stubs that print the loaded config.
"""

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from digest import store
from digest.config import Config, ConfigError, load_config, summarise_config
from digest.fetch import fetch_all
from digest.models import Item, SourceHealth
from digest.store import StoreError

DEFAULT_DB_FILENAME = "digest.db"

log = logging.getLogger(__name__)

STAGES = {
    "fetch": "pull items from every enabled source, normalise them, and store what is new",
    "rank": "score stored items against the interest profile and apply per-topic quotas",
    "render": "write the selected items to digests/YYYY-MM-DD.md",
    "run": "fetch, rank, summarise and render in one pass -- the scheduled entry point",
}


def _force_utf8_output() -> None:
    """Stop a non-ASCII headline from killing an unattended run.

    On Windows, sys.stdout defaults to the locale codepage (cp1252 here), so printing a
    title containing CJK, emoji or most non-Latin-1 text raises UnicodeEncodeError and takes
    the whole run down. HN carries such titles routinely. `errors="replace"` on top of utf-8
    means even a terminal that cannot render a glyph degrades to `?` rather than crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _db_path(args: argparse.Namespace) -> Path:
    """`--db`, else $DIGEST_DB_PATH, else ./digest.db."""
    if args.db is not None:
        return Path(args.db)
    return Path(os.environ.get("DIGEST_DB_PATH") or DEFAULT_DB_FILENAME)


def _run_fetch(config: Config, args: argparse.Namespace) -> int:
    """Fetch every enabled source, persist what is new, and report."""
    result = asyncio.run(fetch_all(config))

    for item in result.items:
        print(f"[{item.source}] {item.title} — {item.url}")
    print()

    if args.dry_run:
        print(f"fetched {len(result.items)}, new -, dupes -  (--dry-run: nothing written)")
        for record in result.health:
            _log_source_line(record, result, fetched=_count_for(result.items, record.name))
        return 0

    conn = store.init_db(_db_path(args))
    try:
        run_started_at = datetime.now(tz=UTC)
        total_new = total_dupes = 0

        # Persisted per source so the run log can report per-source counts. One shared
        # `run_started_at` across every batch keeps "this run" exactly queryable.
        for record in result.health:
            source_items = [i for i in result.items if i.source == record.name]
            new = store.upsert_items(conn, source_items, now=run_started_at)
            dupes = store.count_dupes(conn, run_started_at, source=record.name)
            total_new += new
            total_dupes += dupes
            store.record_source_health(conn, record)
            _log_source_line(record, result, fetched=len(source_items), new=new, dupes=dupes)

        print(f"fetched {len(result.items)}, new {total_new}, dupes {total_dupes}")
        print()
        # Read back rather than reusing the in-memory records: the persisted counter is the
        # one that knows a source has been failing for three days.
        print(_health_summary(result.items, store.get_source_health(conn), result.notes))
    finally:
        conn.close()
    return 0


def _run_render(config: Config, args: argparse.Namespace) -> int:
    """Print everything not yet shown in a digest, grouped by source.

    Plain text for now; the jinja2 Markdown template is phase 3c, which is also when
    `store.stamp_digest_date` gets wired in. Until then this is deliberately non-destructive
    -- running it does not consume the items.
    """
    conn = store.init_db(_db_path(args))
    try:
        items = store.new_items(conn)
        health = store.get_source_health(conn)
    finally:
        conn.close()

    if not items:
        print("Nothing new. Run `digest fetch` first.")
        return 0

    by_source: dict[str, list[Item]] = {}
    for item in items:
        by_source.setdefault(item.source, []).append(item)

    print(f"{len(items)} new item(s) across {len(by_source)} source(s)\n")
    for source_name, source_items in by_source.items():
        print(f"## {source_name} ({len(source_items)})")
        for item in source_items:
            when = item.published_at.strftime("%Y-%m-%d %H:%M") if item.published_at else "  ?  "
            print(f"  {when}  {item.title}")
            print(f"{'':>17}{item.url}")
        print()

    print(_health_summary(items, health))
    print("\n(digest_date is not stamped until phase 3c, so these stay 'new'.)")
    return 0


def _count_for(items: list[Item], source_name: str) -> int:
    return sum(1 for item in items if item.source == source_name)


def _log_source_line(
    record: SourceHealth,
    result: object,
    fetched: int,
    new: int | None = None,
    dupes: int | None = None,
) -> None:
    """CLAUDE.md: one line per source per run -- name, fetched, new, duration, ok/failed.

    Emitted here rather than in fetch.py because the "new" count only exists once the store
    has written, and fetch.py may not touch the store. The duration travels over on
    `FetchResult.durations`.
    """
    duration = getattr(result, "durations", {}).get(record.name, 0.0)
    log.info(
        "source=%s fetched=%d new=%s dupes=%s duration=%.2fs status=%s",
        record.name,
        fetched,
        "-" if new is None else new,
        "-" if dupes is None else dupes,
        duration,
        "ok" if record.consecutive_failures == 0 else "failed",
    )


def _health_summary(
    items: list[Item],
    health: list[SourceHealth],
    notes: dict[str, list[str]] | None = None,
) -> str:
    """The source-health footer from PLAN.md section 2.1.

    Per-feed notes are indented under their source. For a bundle like `ai_blogs` the
    source-level "ok" is not the whole truth -- five feeds can answer while the sixth is
    four months stale, and that line is the only place it shows.
    """
    counts: dict[str, int] = {}
    for item in items:
        counts[item.source] = counts.get(item.source, 0) + 1

    lines = [f"{len(items)} items from {len(health)} source(s):"]
    for record in health:
        ok = record.consecutive_failures == 0
        status = "ok" if ok else f"FAILED x{record.consecutive_failures}"
        seen = (
            record.last_success_at.isoformat(timespec="seconds")
            if record.last_success_at
            else "never"
        )
        lines.append(
            f"  {record.name:<12} {status:<12} items={counts.get(record.name, 0):<4} "
            f"last_success={seen}"
        )
        for note in (notes or {}).get(record.name, []):
            lines.append(f"      - {note}")
    return "\n".join(lines)


def _not_implemented(command: str, config: Config, args: argparse.Namespace) -> int:
    print(f"digest {command}: not implemented")
    print(f"  will: {STAGES[command]}")
    if getattr(args, "dry_run", False):
        print("  (--dry-run requested; nothing would be written)")
    print()
    print(summarise_config(config))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="digest",
        description="Personal daily AI/tech news digest.",
    )

    # Shared by every subcommand. On a parent parser rather than the top level so that
    # `digest fetch --config-dir X` works, which is the order people actually type.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory holding sources.yaml and interests.yaml (default: current directory).",
    )
    common.add_argument(
        "--dry-run",
        action="store_true",
        help="Do the work but write nothing.",
    )
    common.add_argument(
        "--db",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"SQLite database (default: $DIGEST_DB_PATH, else ./{DEFAULT_DB_FILENAME}).",
    )
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show the per-source run log on stderr.",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="{fetch,rank,render,run}")
    for name, description in STAGES.items():
        sub = subparsers.add_parser(
            name,
            parents=[common],
            help=description,
            description=description,
        )
        sub.set_defaults(command=name)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    _force_utf8_output()

    # The run log goes to stderr so stdout stays pipeable to a file or a pager.
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        config = load_config(args.config_dir)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    handlers = {"fetch": _run_fetch, "render": _run_render}
    handler = handlers.get(args.command)
    if handler is None:
        return _not_implemented(args.command, config, args)

    try:
        return handler(config, args)
    except (StoreError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
