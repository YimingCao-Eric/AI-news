"""`digest` command line entry point.

Subcommands are separate from the start so ranking can be iterated on without re-fetching
(PLAN.md section 7, phase 0). `fetch` is live as of phase 1; `rank`, `render` and `run`
remain stubs that print the loaded config.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from digest.config import Config, ConfigError, load_config, summarise_config
from digest.fetch import fetch_all
from digest.models import Item, SourceHealth

STAGES = {
    "fetch": "pull items from every enabled source and normalise them into Item objects",
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


def _run_fetch(config: Config, args: argparse.Namespace) -> int:
    """Fetch every enabled source and print what came back.

    Phase 1 prints and discards; persistence is phase 2, which is also when `--dry-run`
    starts to mean something here.
    """
    items, health = asyncio.run(fetch_all(config))

    for item in items:
        print(f"[{item.source}] {item.title} — {item.url}")

    print()
    print(_health_summary(items, health))
    if args.dry_run:
        print("(--dry-run: nothing is persisted in phase 1 either way)")
    return 0


def _health_summary(items: list[Item], health: list[SourceHealth]) -> str:
    """The source-health footer from PLAN.md section 2.1, in its phase 1 form."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item.source] = counts.get(item.source, 0) + 1

    lines = [f"{len(items)} items from {len(health)} enabled source(s):"]
    for record in health:
        ok = record.consecutive_failures == 0
        status = "ok" if ok else "FAILED"
        seen = (
            record.last_success_at.isoformat(timespec="seconds")
            if record.last_success_at
            else "never"
        )
        lines.append(
            f"  {record.name:<12} {status:<6} items={counts.get(record.name, 0):<4} "
            f"last_success={seen}"
        )
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

    if args.command == "fetch":
        return _run_fetch(config, args)
    return _not_implemented(args.command, config, args)


if __name__ == "__main__":
    raise SystemExit(main())
