"""Run the whole pipeline over recorded fixtures and dump the result deterministically.

fetch -> normalise -> store, with the real `fetch_all`, the real `init_db`, the real
`upsert_items`. Not the adapters in isolation: the point is the *composition*, which is what
the unit tests do not cover.

Byte-identical across runs and across machines. Everything that could vary is either pinned
or excluded, and the exclusion list is deliberately short -- anything excluded here is
something the snapshot stops testing forever, so exclusions are reserved for environment
dependencies being injected on purpose, never for source data we control.

**Pinned**
  * the clock, to `captured_at` from tests/fixtures/manifest.json (see `snapshot_now`)
  * the database, to a temp file that is deleted afterwards

**Excluded from the dump**
  * `first_seen_at` -- the run's own clock
  * `SourceHealth.last_success_at`, per-source durations -- ditto
  * the temp database path -- machine-dependent

Usage:

    uv run python scripts/snapshot_pipeline.py            # write tests/snapshots/pipeline.txt
    uv run python scripts/snapshot_pipeline.py --stdout   # print instead
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from digest import store
from digest.adapters import ai_blogs
from digest.config import Config, load_config
from digest.fetch import fetch_all

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SNAPSHOT_PATH = REPO_ROOT / "tests" / "snapshots" / "pipeline.txt"
MANIFEST_PATH = FIXTURES / "manifest.json"


class SnapshotError(BaseException):
    """The snapshot cannot be generated faithfully.

    Derives from BaseException, not Exception, for the same reason
    `tests.conftest.NetworkAccessInTestError` does -- and this recurrence is itself the
    argument for the idiom.

    A missing fixture route raises inside an adapter, where `fetch.py` catches every
    `Exception` so that no source can abort a run. As a RuntimeError this was duly caught,
    filed as "source failed", and `generate()` carried on to emit a *quietly smaller
    snapshot* -- a broken harness reported as a dead feed. Correct production behaviour
    producing a wrong answer for a test tool.

    Outside `Exception`, it propagates through the resilience layer and fails loudly, which
    is what a broken harness should do.
    """


def snapshot_now() -> datetime:
    """The pinned clock: when this fixture set was captured.

    Derived from the manifest rather than hardcoded, because it is a property of the
    *fixtures*, not of the project. `ai_blogs` filters entries against
    `now - MAX_ENTRY_AGE_DAYS`, so a constant frozen at one date would push every entry
    outside the cutoff once the fixtures were refreshed -- yielding a snapshot of nothing,
    produced by a test that passes. Re-recording now updates the clock as a side effect.
    """
    if not MANIFEST_PATH.exists():
        raise SnapshotError(
            f"{MANIFEST_PATH} is missing. Generate it with "
            f"`uv run python scripts/record_fixtures.py --manifest-only`, or re-record the "
            f"fixtures, which writes it."
        )
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return datetime.fromisoformat(manifest["captured_at"]).astimezone(UTC)


# ------------------------------------------------------------------------- fixture routing

#: URL path -> fixture filename. Keyed on **path**, not full URL: the HN request carries
#: `created_at_i>{since_ts}` recomputed from the clock, so its full URL is never the same
#: twice and exact-URL routing would fail intermittently.
ROUTES: dict[str, str] = {
    "/api/v1/search_by_date": "hn_search_by_date.json",
    "/api/daily_papers": "hf_papers.json",
    "/trending": "gh_trending.html",
    "/rss/cs.AI": "arxiv_cs_ai.xml",
    "/rss/cs.CL": "arxiv_cs_cl.xml",
    "/rss/cs.MA": "arxiv_cs_ma.xml",
}


def _ai_blogs_routes(config: Config) -> dict[str, str]:
    """ai_blogs feeds are configured, so their routes follow sources.yaml."""
    feeds = config.sources.by_name("ai_blogs").feeds or []
    return {urlsplit(feed.url).path: f"ai_blogs_{feed.name}.xml" for feed in feeds}


def _handler(routes: dict[str, str]) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        name = routes.get(path)
        if name is None:
            raise SnapshotError(
                f"no fixture routed for {request.url}. Add its path to ROUTES in "
                f"scripts/snapshot_pipeline.py, and record it with record_fixtures.py."
            )
        return httpx.Response(200, text=(FIXTURES / name).read_text(encoding="utf-8"))

    return handle


@contextmanager
def offline(config: Config, now: datetime) -> Iterator[None]:
    """Pin the clock and serve every request from a fixture.

    `fetch_all` builds its own `httpx.AsyncClient` with no injection point, so the transport
    has to be patched at the module. That is a layering finding in its own right (R1/R3);
    adding the seam now would mean refactoring the composition before the snapshot that
    protects it exists.
    """
    routes = {**ROUTES, **_ai_blogs_routes(config)}
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(_handler(routes))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            httpx, "AsyncClient", lambda **kw: real_client(**{**kw, "transport": transport})
        )
        patch.setattr(ai_blogs, "_now", lambda: now)
        yield


# ------------------------------------------------------------------------------- the dump


def _row_line(row: sqlite3.Row) -> str:
    raw_hash = hashlib.sha256(
        # sort_keys so an incidental key-order change cannot churn the snapshot.
        json.dumps(json.loads(row["raw_json"] or "{}"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return "  ".join(
        (
            row["url_hash"][:12],
            f"{row['source']:<12}",
            row["published_at"] or "-",
            (row["dupe_of"] or "-")[:12],
            raw_hash[:12],
            # JSON-escaped: a title containing a newline or tab cannot corrupt a line.
            json.dumps(row["title"], ensure_ascii=False),
            row["url"],
        )
    )


def generate(config: Config | None = None) -> str:
    """Run the pipeline over the fixtures and return the dump."""
    config = config or load_config(REPO_ROOT)
    now = snapshot_now()

    import asyncio

    with offline(config, now):
        result = asyncio.run(fetch_all(config))

    _assert_time_window_is_exercised(result.items, config)

    with tempfile.TemporaryDirectory() as tmp:
        conn = store.init_db(Path(tmp) / "snapshot.db")
        try:
            store.upsert_items(conn, result.items, now=now)
            rows = conn.execute(
                "SELECT url_hash, url, title, source, published_at, dupe_of, raw_json "
                "FROM items ORDER BY url_hash"
            ).fetchall()
            lines = [_row_line(row) for row in rows]
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["source"]] = counts.get(row["source"], 0) + 1
        finally:
            conn.close()

    header = [
        "# pipeline snapshot -- generated by scripts/snapshot_pipeline.py",
        "# fetch -> store over tests/fixtures/, clock pinned to manifest captured_at",
        f"# clock={now.isoformat()}",
        f"# rows={len(rows)}  " + " ".join(f"{k}={counts[k]}" for k in sorted(counts)),
        "# url_hash     source        published_at               dupe_of       raw_sha  title  url",
    ]
    return "\n".join([*header, *lines]) + "\n"


def _assert_time_window_is_exercised(items: list, config: Config) -> None:
    """The pinned clock must leave `ai_blogs` genuinely filtering -- not 0, not everything.

    If every entry falls inside the cutoff, or none does, the snapshot stops exercising the
    time window at all and the F2 bug it exists to pin could return unnoticed.
    """
    kept = sum(1 for item in items if item.source == "ai_blogs")
    total = 0
    for feed in config.sources.by_name("ai_blogs").feeds or []:
        import feedparser

        text = (FIXTURES / f"ai_blogs_{feed.name}.xml").read_text(encoding="utf-8")
        total += len(feedparser.parse(text).entries)

    if kept == 0 or kept >= total:
        raise SnapshotError(
            f"ai_blogs kept {kept} of {total} entries at the pinned clock "
            f"({snapshot_now().isoformat()}). The snapshot must exercise the age cutoff: "
            f"0 means the fixtures have aged past it entirely, and keeping all of them means "
            f"the filter is doing nothing. Re-record the fixtures so captured_at moves with "
            f"them."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snapshot_pipeline", description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing.")
    args = parser.parse_args(argv)

    dump = generate()
    if args.stdout:
        # CLAUDE.md: stdout defaults to the locale codepage on Windows. Real titles in these
        # fixtures contain a non-breaking hyphen (U+2011), which cp1252 cannot encode -- this
        # crashed on the first run. The same rule that covers file writes covers this.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.write(dump)
        return 0

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(dump, encoding="utf-8")
    print(f"wrote {SNAPSHOT_PATH.relative_to(REPO_ROOT)} ({len(dump.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
