"""Run the enabled adapters concurrently and collect their items.

This module owns the guarantee that CLAUDE.md makes twice: **one failing source must never
abort a run**. Every adapter call is wrapped, every exception becomes a failed
`SourceOutcome`, and the run continues. It also owns the one-line-per-source run log, so adapters
stay silent.
"""

import asyncio
import logging
import os
import time
from collections.abc import Mapping, Sequence
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
from digest.errors import AdapterError, NoAdapterRegistered
from digest.models import Item, SourceOutcome

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
    #: One per enabled source: what this run did to it. An event, not accumulated state --
    #: `store.get_source_health` owns the latter and is the only thing that can count.
    outcomes: list[SourceOutcome] = field(default_factory=list)
    #: Wall-clock seconds per source name. Measured here because only the fetch loop sees it.
    durations: dict[str, float] = field(default_factory=dict)
    #: Per-source diagnostics from `Adapter.drain_notes` -- per-feed counts, failures and
    #: staleness for bundle sources. The state between "worked" and "failed", which
    #: neither Item nor SourceOutcome can express.
    notes: dict[str, list[str]] = field(default_factory=dict)


#: Adapter *classes* by the `kind` they implement -- not instances, and not keyed by source.
#:
#: The registry used to hold one long-lived instance per source name, which conflated two
#: things: which implementation to use, and which source it serves. One class could therefore
#: serve exactly one source, so a second RSS source wanting identical behaviour needed a
#: subclass whose only content was a different name -- and PLAN.md section 2 Tier 2 is mostly
#: more RSS. The shortcut, registering one instance under two names, silently shared that
#: instance's `_notes` between two concurrently-fetched sources.
#:
#: `fetch_all` constructs one instance per source per run, so per-run adapter state is
#: private by construction rather than by an invariant nobody can see.
IMPLEMENTATIONS: dict[str, type[Adapter]] = {
    cls.kind: cls
    for cls in (
        HNAdapter,
        GhTrendingAdapter,
        HFPapersAdapter,
        AIBlogsAdapter,
        ArxivAdapter,
    )
}

#: The set `config.load_sources` validates `kind` against. Injected there rather than
#: imported, because `adapters/base.py` imports `Source` from `config` and reaching back
#: would be a cycle.
KNOWN_KINDS: frozenset[str] = frozenset(IMPLEMENTATIONS)

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
    client: httpx.AsyncClient, source: Source, adapter: Adapter | None
) -> tuple[list[Item], SourceOutcome, float, list[str]]:
    """Fetch one source with the instance built for it. Never raises."""
    started = time.monotonic()
    items: list[Item] = []
    notes: list[str] = []
    error: str | None = None
    expected = False

    try:
        if adapter is None:
            # An enabled source with no implementation is a real misconfiguration, not
            # something to skip quietly -- it would otherwise look like a source that
            # returns nothing. `load_sources` rejects an unknown `kind` at config load, so
            # reaching here means a Config assembled in code rather than read from disk.
            raise NoAdapterRegistered(
                f"no adapter implements kind {source.kind!r} for enabled source "
                f"{source.name!r}; known kinds: {', '.join(sorted(IMPLEMENTATIONS)) or '(none)'}"
            )
        async with asyncio.timeout(SOURCE_TIMEOUT_SECONDS):
            items = await adapter.fetch(client, source)
    # Deliberately broad, and deliberately NOT narrowed to AdapterError: CLAUDE.md requires
    # that no source can abort the run, so anything an adapter can raise -- including bugs in
    # the adapter itself -- has to be contained. Narrowing to the named base would let an
    # accidental TypeError escape this handler and bypass both the health record and the
    # `finally` notes drain, which is the opposite of what the taxonomy is for.
    #
    # The named base CLASSIFIES what was caught. It never changes what is caught.
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        expected = isinstance(exc, AdapterError)
        if expected:
            # The adapter anticipated this: a 403, a changed payload, every feed empty.
            # Actionable as "check the source", and routine enough not to want a traceback.
            log.warning("source %s failed: %s", source.name, error)
        else:
            # Nobody anticipated this, so it is a bug in our code until proven otherwise.
            # Previously indistinguishable from the line above, which is how a coding mistake
            # filed itself as a dead feed and cost you a morning checking a healthy source.
            log.error(
                "source %s CRASHED (this is a bug in the adapter, not the source): %s",
                source.name,
                error,
                exc_info=True,
            )
    finally:
        # Drained even on failure: a bundle source that raised because every feed died still
        # knows *which* feeds died, and that is the useful half of the report. Drained from
        # the instance we fetched with, not looked up again -- with one instance per source
        # there is nothing to look up, and nothing to accidentally drain from a sibling.
        if adapter is not None:
            notes = adapter.drain_notes()

    duration = time.monotonic() - started
    finished_at = datetime.now(tz=UTC)
    # Exactly one instant, and no counts: `SourceOutcome` cannot express "failed, but here is
    # when it last worked", and the store computes every counter from the row it already has.
    outcome = (
        SourceOutcome(name=source.name, succeeded_at=finished_at)
        if error is None
        else SourceOutcome(
            name=source.name, failed_at=finished_at, error=error, expected_failure=expected
        )
    )

    # CLAUDE.md's one-line-per-source run log is emitted by the caller, not here: the "new
    # items" column only exists after the store has written, and fetch.py is not allowed to
    # touch the store. `durations` on FetchResult is how the timing reaches it.
    return items, outcome, duration, notes


def build_adapters(
    sources: Sequence[Source], overrides: Mapping[str, Adapter] | None = None
) -> dict[str, Adapter | None]:
    """One adapter instance per source, keyed by source name.

    `overrides` **merges**: an injected entry replaces the instance for that source name and
    every other source is constructed normally, so a test pinning one source's clock does not
    have to supply the other four.

    `None` for a source whose `kind` has no implementation, so `_fetch_one` can record it as
    a failure rather than the run dying before any source is fetched.
    """
    overrides = overrides or {}
    built: dict[str, Adapter | None] = {}
    for source in sources:
        if source.name in overrides:
            built[source.name] = overrides[source.name]
            continue
        implementation = IMPLEMENTATIONS.get(source.kind)
        built[source.name] = implementation() if implementation is not None else None
    return built


async def fetch_all(
    config: Config, *, adapters: Mapping[str, Adapter] | None = None
) -> FetchResult:
    """Fetch every enabled source concurrently.

    Returns everything that succeeded plus an outcome and a duration per source, in
    config order. Sources that are disabled in sources.yaml are not fetched and get no health
    outcome -- the config is the single source of truth for whether a source runs, which is
    why neither `SourceOutcome` nor `SourceHealth` has an `enabled` flag of its own.
    """
    sources = config.sources.enabled
    if not sources:
        log.warning("no sources are enabled in sources.yaml; nothing to fetch")
        return FetchResult()

    built = build_adapters(sources, adapters)

    headers = {"User-Agent": user_agent()}
    timeout = httpx.Timeout(SOURCE_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(_fetch_one(client, source, built[source.name]) for source in sources),
            return_exceptions=True,
        )

    items: list[Item] = []
    outcomes: list[SourceOutcome] = []
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
            outcomes.append(
                SourceOutcome(
                    name=source.name,
                    failed_at=datetime.now(tz=UTC),
                    error=f"{type(result).__name__}: {result}",
                    # Reaching here means the wrapper itself broke, which nobody anticipated.
                    expected_failure=False,
                )
            )
            durations[source.name] = 0.0
            continue
        source_items, outcome, duration, source_notes = result
        items.extend(source_items)
        outcomes.append(outcome)
        durations[source.name] = duration
        if source_notes:
            notes[source.name] = source_notes

    return FetchResult(items=items, outcomes=outcomes, durations=durations, notes=notes)
