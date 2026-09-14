"""Run the enabled adapters concurrently and collect their items.

This module owns the guarantee that CLAUDE.md makes twice: **one failing source must never
abort a run**. Every adapter call is wrapped, every exception becomes a `SourceHealth`
record, and the run continues. It also owns the one-line-per-source run log, so adapters
stay silent.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from digest.adapters.ai_blogs import AIBlogsAdapter
from digest.adapters.arxiv import ArxivAdapter
from digest.adapters.base import Adapter
from digest.adapters.gh_trending import GhTrendingAdapter
from digest.adapters.hf_papers import HFPapersAdapter
from digest.adapters.hn import HNAdapter
from digest.config import Config, Source
from digest.models import Item, SourceHealth

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    """What one run of `fetch_all` produced.

    A named object rather than a tuple: this return value has already grown once (durations,
    so the caller can emit CLAUDE.md's one-line-per-source log *after* persisting, which is
    the only point at which the "new items" count exists), and there are several call sites.
    A tuple that has grown once grows again.
    """

    items: list[Item] = field(default_factory=list)
    health: list[SourceHealth] = field(default_factory=list)
    #: Wall-clock seconds per source name. Measured here because only the fetch loop sees it.
    durations: dict[str, float] = field(default_factory=dict)
    #: Per-source diagnostics from `Adapter.drain_notes` -- per-feed counts, failures and
    #: staleness for bundle sources. The state between "worked" and "failed", which
    #: neither Item nor SourceHealth can express.
    notes: dict[str, list[str]] = field(default_factory=dict)


#: Adapters by the source name in sources.yaml they serve. Keys must match `name` there;
#: an enabled source with no adapter is a recorded failure, not a silent skip.
ADAPTERS: dict[str, Adapter] = {
    adapter.name: adapter
    for adapter in (
        HNAdapter(),
        GhTrendingAdapter(),
        HFPapersAdapter(),
        AIBlogsAdapter(),
        ArxivAdapter(),
    )
}

DEFAULT_USER_AGENT = "AI-news-digest/0.1 (+https://github.com/YimingCao-Eric/AI-news)"

#: Backstop budget for one whole source, not one request.
#:
#: Bundle sources do NOT rely on this: `ai_blogs` (6 feeds, 8s each) and `arxiv_cs_ai`
#: (3 categories, 10s each) enforce their own per-feed timeouts and return partial results,
#: because one slow feed spending the shared budget would cost you every other feed behind
#: the same source. This remains the outer guard for a hung adapter.
SOURCE_TIMEOUT_SECONDS = 20.0


def user_agent() -> str:
    """A real UA is mandatory: GitHub Trending and Reddit both throttle the defaults."""
    return os.environ.get("DIGEST_USER_AGENT") or DEFAULT_USER_AGENT


async def _fetch_one(
    client: httpx.AsyncClient, source: Source
) -> tuple[list[Item], SourceHealth, float, list[str]]:
    """Fetch one source. Never raises: every failure becomes a health record."""
    started = time.monotonic()
    items: list[Item] = []
    notes: list[str] = []
    error: str | None = None

    try:
        adapter = ADAPTERS.get(source.name)
        if adapter is None:
            # An enabled source with no adapter is a real misconfiguration, not something to
            # skip quietly -- it would otherwise look like a source that returns nothing.
            raise LookupError(
                f"no adapter registered for enabled source {source.name!r}; "
                f"registered: {', '.join(sorted(ADAPTERS)) or '(none)'}"
            )
        async with asyncio.timeout(SOURCE_TIMEOUT_SECONDS):
            items = await adapter.fetch(client, source)
    # Deliberately broad: CLAUDE.md requires that no source can abort the run, so anything
    # an adapter can raise -- including bugs in the adapter itself -- has to be contained.
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.warning("source %s failed: %s", source.name, error, exc_info=True)
    finally:
        # Drained even on failure: a bundle source that raised because every feed died still
        # knows *which* feeds died, and that is the useful half of the report.
        reporting_adapter = ADAPTERS.get(source.name)
        if reporting_adapter is not None:
            notes = reporting_adapter.drain_notes()

    duration = time.monotonic() - started
    if error is None:
        health = SourceHealth(name=source.name, last_success_at=datetime.now(tz=UTC))
    else:
        # A flag, not a count. `store.record_source_health` owns the real consecutive-failure
        # counter, because only it can see the previous value.
        health = SourceHealth(name=source.name, consecutive_failures=1)

    # CLAUDE.md's one-line-per-source run log is emitted by the caller, not here: the "new
    # items" column only exists after the store has written, and fetch.py is not allowed to
    # touch the store. `durations` on FetchResult is how the timing reaches it.
    return items, health, duration, notes


async def fetch_all(config: Config) -> FetchResult:
    """Fetch every enabled source concurrently.

    Returns everything that succeeded plus a health record and a duration per source, in
    config order. Sources that are disabled in sources.yaml are not fetched and get no health
    record -- the config is the single source of truth for whether a source runs, which is
    why `SourceHealth` has no `enabled` flag of its own.
    """
    sources = config.sources.enabled
    if not sources:
        log.warning("no sources are enabled in sources.yaml; nothing to fetch")
        return FetchResult()

    headers = {"User-Agent": user_agent()}
    timeout = httpx.Timeout(SOURCE_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(_fetch_one(client, source) for source in sources),
            return_exceptions=True,
        )

    items: list[Item] = []
    health: list[SourceHealth] = []
    durations: dict[str, float] = {}
    notes: dict[str, list[str]] = {}
    for source, result in zip(sources, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                # KeyboardInterrupt, SystemExit, CancelledError: the *run* is being stopped.
                # Recording that as "this feed is unhealthy" would be a lie, and would leave
                # a Ctrl-C looking like source rot in tomorrow's health footer. The
                # never-abort rule is about sources failing, not about the operator quitting.
                raise result
            # Belt and braces: _fetch_one catches every Exception, so reaching here means a
            # bug in the wrapper itself. Still must not abort the run.
            log.error("source %s failed outside the adapter wrapper", source.name, exc_info=result)
            health.append(SourceHealth(name=source.name, consecutive_failures=1))
            durations[source.name] = 0.0
            continue
        source_items, source_health, duration, source_notes = result
        items.extend(source_items)
        health.append(source_health)
        durations[source.name] = duration
        if source_notes:
            notes[source.name] = source_notes

    return FetchResult(items=items, health=health, durations=durations, notes=notes)
