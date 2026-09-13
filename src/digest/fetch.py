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
from datetime import UTC, datetime

import httpx

from digest.adapters.base import Adapter
from digest.adapters.hn import HNAdapter
from digest.config import Config, Source
from digest.models import Item, SourceHealth

log = logging.getLogger(__name__)

#: Adapters by the source name in sources.yaml they serve. Phase 3 adds the other four.
ADAPTERS: dict[str, Adapter] = {adapter.name: adapter for adapter in (HNAdapter(),)}

DEFAULT_USER_AGENT = "AI-news-digest/0.1 (+https://github.com/YimingCao-Eric/AI-news)"

#: Budget for one whole source, not one request.
#:
#: NOTE FOR PHASE 3A (ai_blogs): this wraps the entire source coroutine, which is right for a
#: single-endpoint source but wrong once six feeds sit behind one `Source`. There, one slow
#: feed would burn the shared budget and cost you the other five. That adapter needs its own
#: per-feed timeout and partial-success handling internally -- returning the feeds that did
#: answer -- rather than relying on this outer wrapper.
SOURCE_TIMEOUT_SECONDS = 20.0


def user_agent() -> str:
    """A real UA is mandatory: GitHub Trending and Reddit both throttle the defaults."""
    return os.environ.get("DIGEST_USER_AGENT") or DEFAULT_USER_AGENT


async def _fetch_one(client: httpx.AsyncClient, source: Source) -> tuple[list[Item], SourceHealth]:
    """Fetch one source. Never raises: every failure becomes a health record."""
    started = time.monotonic()
    items: list[Item] = []
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

    duration = time.monotonic() - started
    if error is None:
        health = SourceHealth(name=source.name, last_success_at=datetime.now(tz=UTC))
    else:
        # Genuinely *consecutive* failures need the previously stored count, which arrives
        # with the store in phase 2. Until then this is 1 for "failed on this run", not a
        # running total -- honest about the current run and no more.
        health = SourceHealth(name=source.name, consecutive_failures=1)

    # CLAUDE.md: one line per source per run -- name, items fetched, new items, duration,
    # ok/failed. "new" needs the database to mean anything, so it is `-` until phase 2.
    log.info(
        "source=%s fetched=%d new=%s duration=%.2fs status=%s%s",
        source.name,
        len(items),
        "-",
        duration,
        "ok" if error is None else "failed",
        "" if error is None else f" error={error}",
    )
    return items, health


async def fetch_all(config: Config) -> tuple[list[Item], list[SourceHealth]]:
    """Fetch every enabled source concurrently.

    Returns everything that succeeded plus a health record per source, in config order.
    Sources that are disabled in sources.yaml are not fetched and get no health record --
    the config is the single source of truth for whether a source runs, which is why
    `SourceHealth` no longer carries an `enabled` flag of its own.
    """
    sources = config.sources.enabled
    if not sources:
        log.warning("no sources are enabled in sources.yaml; nothing to fetch")
        return [], []

    headers = {"User-Agent": user_agent()}
    timeout = httpx.Timeout(SOURCE_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(_fetch_one(client, source) for source in sources),
            return_exceptions=True,
        )

    items: list[Item] = []
    health: list[SourceHealth] = []
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
            continue
        source_items, source_health = result
        items.extend(source_items)
        health.append(source_health)

    return items, health
