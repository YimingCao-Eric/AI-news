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
from digest.errors import DigestControlError
from digest.fetch import KNOWN_KINDS, FetchResult, fetch_all
from digest.models import Item, SourceHealth, SourceOutcome
from digest.store import StoreError

DEFAULT_DB_FILENAME = "digest.db"

# --- Exit codes. A scheduler decides "did it work?" from this alone. ----------------------
#
# Verified against the CLI rather than assumed: argparse hardcodes 2 in `parser.error()`, so
# 2 is not ours to reassign. Rather than fight it, 2 means "the invocation or the config is
# wrong" -- both are "a human must fix this; retrying will not help", which is the only
# distinction a scheduler can act on.
#
# Partial failure is deliberately 0. Sources fail routinely, and a code that fires most
# mornings gets filtered -- the same reasoning that gave ai_blogs per-feed staleness
# thresholds instead of one global number. "Three of five died" is carried by the footer and
# the per-source log line, which is why SF-2 made that line visible without -v.
#
# `DigestControlError` gets 70 (EX_SOFTWARE) rather than falling into 4. The distinction is
# actionable: 4 means the environment failed and tomorrow may work, so retry; 70 means the
# program's own assumptions are broken, so retrying cannot help and a human must look. It
# should never occur in production at all.
EXIT_OK = 0
EXIT_USAGE_OR_CONFIG = 2
EXIT_STORAGE = 3
EXIT_ALL_SOURCES_FAILED = 4
EXIT_INTERRUPTED = 130  # 128 + SIGINT, the shell convention cron and CI already understand
EXIT_INTERNAL_ERROR = 70  # EX_SOFTWARE: a DigestControlError reached the top. A bug.
# ------------------------------------------------------------------------------------------

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
        print(f"fetched {len(result.items)}, inserted -, dupes -  (--dry-run: nothing written)")
        for outcome in result.outcomes:
            _log_source_line(outcome, result, fetched=_count_for(result.items, outcome.name))
        return _exit_code_for(result)

    conn = store.init_db(_db_path(args))
    try:
        run_started_at = datetime.now(tz=UTC)
        total_new = total_dupes = 0

        # Persisted per source so the run log can report per-source counts. One shared
        # `run_started_at` across every batch keeps "this run" exactly queryable.
        for outcome in result.outcomes:
            source_items = [i for i in result.items if i.source == outcome.name]
            new = store.insert_items(conn, source_items, now=run_started_at)
            dupes = store.count_dupes(conn, run_started_at, source=outcome.name)
            total_new += new
            total_dupes += dupes
            # Both timestamps forwarded as-is: exactly one is set, so there is no outcome to
            # deduce here. Deducing it at the call site would have moved the bug, not fixed it.
            store.record_source_health(
                conn,
                outcome.name,
                succeeded_at=outcome.succeeded_at,
                failed_at=outcome.failed_at,
            )
            _log_source_line(outcome, result, fetched=len(source_items), new=new, dupes=dupes)

        print(f"fetched {len(result.items)}, inserted {total_new}, dupes {total_dupes}")
        print()
        # Read back rather than reusing the in-memory records: the persisted counter is the
        # one that knows a source has been failing for three days.
        print(
            _health_summary(
                result.items,
                store.get_source_health(conn),
                result.notes,
                enabled={s.name for s in config.sources.enabled},
            )
        )
    finally:
        conn.close()
    return _exit_code_for(result)


def _run_render(config: Config, args: argparse.Namespace) -> int:
    """Print everything not yet shown in a digest, grouped by source.

    Plain text for now; the jinja2 Markdown template is phase 3c, which is also when
    `store.stamp_digest_date` gets wired in. Until then this is deliberately non-destructive
    -- running it does not consume the items.
    """
    # `create=False`: sqlite3 will happily invent a database for any path, so a typo'd
    # --db used to yield a brand-new file and a confident "Nothing new" -- the failure and
    # the success printing the same thing.
    conn = store.init_db(_db_path(args), create=False)
    try:
        items = store.unrendered_items(conn)
        health = store.get_source_health(conn)
    finally:
        conn.close()

    if not items:
        print("Nothing waiting for a digest. Run `digest fetch` first.")
        return EXIT_OK

    by_source: dict[str, list[Item]] = {}
    for item in items:
        by_source.setdefault(item.source, []).append(item)

    print(f"{len(items)} item(s) not yet in a digest, across {len(by_source)} source(s)\n")
    for source_name, source_items in by_source.items():
        print(f"## {source_name} ({len(source_items)})")
        for item in source_items:
            when = item.published_at.strftime("%Y-%m-%d %H:%M") if item.published_at else "  ?  "
            print(f"  {when}  {item.title}")
            print(f"{'':>17}{item.url}")
        print()

    print(_health_summary(items, health, enabled={s.name for s in config.sources.enabled}))
    print("\n(digest_date is not stamped until phase 3c, so these stay unrendered.)")
    return EXIT_OK


def _exit_code_for(result: FetchResult) -> int:
    """0 unless *every* enabled source failed.

    The consequence this prevents: seven consecutive total failures would otherwise satisfy
    PLAN.md section 0's "runs unattended for seven consecutive days" criterion while
    delivering nothing, because every one of those runs exited 0.

    Deliberately reads the outcomes rather than touching how failures are caught.
    `fetch.py`'s `except Exception` stays exactly as it is -- narrowing it would reintroduce
    the failure the never-abort rule exists to prevent, one broken source killing the whole
    digest. This is aggregate reporting, downstream of the catch.
    """
    if not result.outcomes:
        return EXIT_OK
    if all(not outcome.succeeded for outcome in result.outcomes):
        return EXIT_ALL_SOURCES_FAILED
    return EXIT_OK


def _count_for(items: list[Item], source_name: str) -> int:
    return sum(1 for item in items if item.source == source_name)


def _log_source_line(
    outcome: SourceOutcome,
    result: FetchResult,
    fetched: int,
    new: int | None = None,
    dupes: int | None = None,
) -> None:
    """CLAUDE.md: one line per source -- name, fetched, inserted, duration, ok/failed.

    `inserted`, not `new`: "new" also names the items `digest render` shows, which are
    the ones never rendered rather than the ones written this run. The two numbers
    routinely disagree and both are right.

    Emitted here rather than in fetch.py because the "new" count only exists once the store
    has written, and fetch.py may not touch the store. The duration travels over on
    `FetchResult.durations`.
    """
    # Indexed, not `getattr(result, "durations", {})`: that default silently reported
    # duration=0.00s forever if the field were ever renamed, which is the exact shape of
    # failure this theme is removing.
    duration = result.durations[outcome.name]
    log.info(
        "source=%s fetched=%d inserted=%s dupes=%s duration=%.2fs status=%s",
        outcome.name,
        fetched,
        "-" if new is None else new,
        "-" if dupes is None else dupes,
        duration,
        _status_word(outcome),
    )


def _status_word(outcome: SourceOutcome) -> str:
    """`ok`, `failed`, or `CRASHED`.

    The distinction VN-4 found missing: a deliberate SourcePayloadError meaning "the API
    returned a dict, not a list" and an accidental TypeError from a coding mistake produced
    the same word. The first means re-record a fixture; the second means revert a commit, and
    you would spend the morning checking a healthy source before discovering which.
    """
    if outcome.succeeded:
        return "ok"
    return "failed" if outcome.expected_failure else "CRASHED"


def _health_summary(
    items: list[Item],
    health: list[SourceHealth],
    notes: dict[str, list[str]] | None = None,
    enabled: set[str] | None = None,
) -> str:
    """The source-health footer from PLAN.md section 2.1.

    Five states, five shapes, because several of them used to render identically:

      ok        ran, succeeded, returned items
      quiet     ran, succeeded, returned nothing -- legitimate for hn and arxiv under
                CLAUDE.md's zero-items rule, and previously indistinguishable from disabled
      FAILED    failed this run, with the history that survives recovery appended
      disabled  in sources.yaml but not enabled; counts suppressed with a dash because there
                are none, while last_success is kept so you can see when it last ran
      (stale)   per-feed lines under a bundle, unchanged

    `enabled` is what makes disabled distinguishable at all: `get_source_health` returns
    every row ever written, so without it the footer asserted "ok, items=0" about sources
    that did not run.

    Partial failure exits 0 by design, which makes this footer the only carrier of "three of
    five sources died". Phase 5's delivery channel has to include it, not just the items, or
    the reasoning that makes 0 correct here makes partial failure invisible there.
    """
    counts: dict[str, int] = {}
    for item in items:
        counts[item.source] = counts.get(item.source, 0) + 1

    ran = enabled if enabled is not None else {record.name for record in health}
    lines = [f"{len(ran)} of {len(health)} sources enabled, {len(items)} items this run:"]

    for record in health:
        fetched = counts.get(record.name, 0)
        if record.name not in ran:
            status, count_text = "disabled", "items=—   "
        elif record.consecutive_failures:
            status, count_text = "FAILED", f"items={fetched:<4}"
        elif fetched:
            status, count_text = "ok", f"items={fetched:<4}"
        else:
            status, count_text = "quiet", f"items={fetched:<4}"

        seen = (
            record.last_success_at.isoformat(timespec="seconds")
            if record.last_success_at
            else "never"
        )
        line = f"  {record.name:<12} {status:<9} {count_text} last_success={seen}"
        if history := _failure_history(record):
            line += f"  {history}"
        lines.append(line)

        for note in (notes or {}).get(record.name, []):
            lines.append(f"      - {note}")
    return "\n".join(lines)


def _failure_history(record: SourceHealth) -> str:
    """`consecutive=N total=M`, with `≥` when the total provably predates counting.

    Spelled out rather than "N of M total", which reads on a half-awake morning as "N out of
    M attempts" instead of "N in a row, M lifetime". The footer is the one surface where
    unambiguous beats elegant.

    `total_failures >= consecutive_failures` holds for every row counted since schema v2, so
    a row where it is *less* is one the v2 migration back-filled as 0 -- it has real history
    the counter never saw. Marking it `≥` states a floor instead of inventing a number, and
    the marker disappears by itself once the true count overtakes. No column, no seeding.

    What no scheme recovers: a source that failed fifty times before v2 and has since
    recovered reads `total=0`, because nothing anywhere remembers. Seeding from
    `consecutive_failures` would not have helped -- that is 0 too, once it recovered.
    """
    if not record.consecutive_failures and not record.total_failures:
        return ""
    consecutive = record.consecutive_failures
    marker = "≥" if record.total_failures < consecutive else ""
    total = max(record.total_failures, consecutive)
    parts = [f"consecutive={consecutive}", f"total={marker}{total}"]
    if record.last_failure_at:
        parts.append(f"last_failure={record.last_failure_at.isoformat(timespec='seconds')}")
    return " ".join(parts)


def _not_implemented(command: str, config: Config, args: argparse.Namespace) -> int:
    print(f"digest {command}: not implemented")
    print(f"  will: {STAGES[command]}")
    if getattr(args, "dry_run", False):
        print("  (--dry-run requested; nothing would be written)")
    print()
    print(summarise_config(config))
    return EXIT_OK


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
        return EXIT_USAGE_OR_CONFIG

    _force_utf8_output()

    # The run log goes to stderr so stdout stays pipeable to a file or a pager.
    #
    # Root stays at WARNING; only `digest` drops to INFO. CLAUDE.md requires one line per
    # source per run, and that line used to sit at INFO under a WARNING default -- so the
    # scheduled 07:00 run, where nobody types -v, wrote it nowhere. Setting the root to INFO
    # instead would drag in httpx's per-request chatter and make the line harder to find than
    # when it was missing.
    logging.basicConfig(format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("digest").setLevel(logging.DEBUG if args.verbose else logging.INFO)

    try:
        config = load_config(args.config_dir, known_kinds=KNOWN_KINDS)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE_OR_CONFIG

    handlers = {"fetch": _run_fetch, "render": _run_render}
    handler = handlers.get(args.command)
    if handler is None:
        return _not_implemented(args.command, config, args)

    try:
        return handler(config, args)
    except (StoreError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_STORAGE
    except DigestControlError:
        # Never expected in production: an assumption about our own execution was violated,
        # not a source misbehaving. The traceback is the whole point, so it is logged rather
        # than summarised -- this is the one failure where you want the stack, not a status.
        log.exception("internal error: the program's own assumptions were violated")
        return EXIT_INTERNAL_ERROR
    except KeyboardInterrupt:
        # Phase 1 established that an operator quitting must not be recorded as a dead feed.
        # The same distinction has to reach the exit status, or a scheduler reads Ctrl-C as
        # "every source failed" and escalates. fetch.py already re-raises non-Exception
        # BaseException rather than filing it as source rot; this is the other end of it.
        print("\ninterrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
