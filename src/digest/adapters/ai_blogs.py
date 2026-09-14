"""Generic RSS/Atom over a bundle of AI company blogs.

This adapter exists to survive the failure mode phase 3a measured on 2026-09-14: the
community mirror these feeds come from is not uniformly unreliable, it is unreliable **per
feed**. Both official feeds were fresh; three of four mirror feeds were between 49 and 115
days stale, every one of them returning HTTP 200 with well-formed XML. One healthy feed from
that mirror tells you nothing about the others.

So a feed here has four outcomes, not two: items, empty, failed, and *stale* -- and the last
is the dangerous one, because nothing about the response says anything is wrong. Every
outcome reaches the run summary through `drain_notes`.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import feedparser
import httpx
from feedparser.util import FeedParserDict

from digest.adapters.base import Adapter
from digest.config import Feed, Source
from digest.models import Item

#: Per-feed budget, deliberately well under fetch.py's 20s whole-source budget.
#:
#: This is the fix for the note left on SOURCE_TIMEOUT_SECONDS in phase 1: the outer wrapper
#: is right for a single-endpoint source and wrong for six, where one slow feed would spend
#: the shared budget and cost you the other five. Six feeds at 8s concurrent still fits
#: comfortably inside 20s even in the worst case.
FEED_TIMEOUT_SECONDS = 8.0

#: How many days without a new entry before a feed is reported STALE.
#:
#: Per feed, not global, and derived from cadence measured 2026-09-14. A global threshold
#: either cries wolf on the slow feeds or sleeps through the fast ones, and a warning that
#: cries wolf gets ignored -- which costs the entire mechanism. Tune a number here when a
#: feed's real cadence turns out to differ; that is a one-line change by design.
#:
#: (Ideally these live in sources.yaml next to the feed. They are here because phase 3a was
#: scoped not to touch config.py, and `Feed` forbids extra keys. Moving them is a small
#: config change whenever that is allowed.)
STALENESS_THRESHOLD_DAYS: dict[str, int] = {
    "openai_news": 7,  # measured 0d old, 1193 entries -- posts constantly
    "deepmind": 21,  # measured 6d old, official feed
    "anthropic_news": 30,  # measured 13d old
    "claude": 14,  # measured 0d old, changelog-like cadence
    "anthropic_research": 60,  # measured 4d old but only 15 entries total -- low volume
    "cursor": 45,  # measured 4d old, 22 entries -- changelog cadence
}
DEFAULT_STALENESS_THRESHOLD_DAYS = 30

#: Ingest cutoff. Entries older than this are not stored.
#:
#: Unlike arXiv, HF papers and GitHub Trending -- all of which publish *today's* list -- these
#: feeds carry their whole back catalogue. Measured 2026-09-14: openai_news alone serves 1193
#: entries going back years, and a first run without this stored 1824 items across six feeds.
#:
#: That is not "fetch generously", it is a one-off archive import of material that can never
#: be selected: `max_age_hours: 48` in interests.yaml means anything older than two days is
#: ineligible for a digest by definition. 30 days keeps 15x headroom for re-scoring
#: experiments against real history while declining four years of it.
#:
#: Entries with no parseable date are KEPT. Fail toward the visible error: showing an item
#: twice is recoverable, dropping one for a missing timestamp is not visible at all.
MAX_ENTRY_AGE_DAYS = 30

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class FeedOutcome:
    """One feed's result. Separates "nothing recent" from "nothing at all".

    `newest` is computed over *every* entry, before the age cutoff is applied. This matters:
    a feed that is 112 days stale has zero entries inside a 30-day window, so measuring
    staleness after filtering would report "0 items" and lose the very signal this adapter
    exists to surface.
    """

    feed: Feed
    items: list[Item]
    total_entries: int
    newest: datetime | None
    not_modified: bool = False


class AIBlogsAdapter(Adapter):
    name = "ai_blogs"

    def __init__(self) -> None:
        self._notes: list[str] = []

    def drain_notes(self) -> list[str]:
        notes, self._notes = self._notes, []
        return notes

    async def fetch(self, client: httpx.AsyncClient, source: Source) -> list[Item]:
        self._notes = []
        feeds = source.endpoints
        now = datetime.now(tz=UTC)

        results = await asyncio.gather(
            *(self._fetch_feed(client, feed, source.name, now) for feed in feeds),
            return_exceptions=True,
        )

        items: list[Item] = []
        failures = 0
        for feed, result in zip(feeds, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                failures += 1
                # Reported, never silently absent: a feed that vanishes without a line in
                # the summary is how a topic quietly stops appearing.
                self._notes.append(f"{feed.name}: FAILED {type(result).__name__}: {result}")
                continue
            self._notes.append(_describe(result, now))
            items.extend(result.items)

        if failures == len(feeds):
            # Every feed down at once is a network or mirror outage, not six quiet blogs.
            raise RuntimeError(
                f"{source.name}: all {failures} feeds failed. Notes: {'; '.join(self._notes)}"
            )

        if source.fetch_limit is not None:
            items = items[: source.fetch_limit]
        return items

    async def _fetch_feed(
        self, client: httpx.AsyncClient, feed: Feed, source_name: str, now: datetime
    ) -> FeedOutcome:
        headers = conditional_headers(feed)
        async with asyncio.timeout(FEED_TIMEOUT_SECONDS):
            response = await client.get(feed.url, headers=headers)

        if response.status_code == 304:
            # Not an error: the server is telling us nothing changed since the validator we
            # sent. Nothing is stored, and the summary says so rather than showing a zero
            # that looks like breakage.
            return FeedOutcome(feed=feed, items=[], total_entries=0, newest=None, not_modified=True)
        response.raise_for_status()

        parsed = feedparser.parse(response.text)
        if parsed.bozo and not parsed.entries:
            raise ValueError(
                f"{source_name}/{feed.name}: unparseable XML from {feed.url} "
                f"({parsed.bozo_exception})"
            )

        # A single quiet blog is normal, so an empty feed yields no items rather than
        # raising. The all-six case is handled by the caller.
        all_items = [
            item
            for entry in parsed.entries
            if (item := _item_from_entry(entry, source_name, feed.name)) is not None
        ]

        # Newest over EVERYTHING, before the cutoff -- see FeedOutcome.
        dated = [item.published_at for item in all_items if item.published_at is not None]
        newest = max(dated) if dated else None

        cutoff = now - timedelta(days=MAX_ENTRY_AGE_DAYS)
        recent = [
            item for item in all_items if item.published_at is None or item.published_at >= cutoff
        ]
        return FeedOutcome(feed=feed, items=recent, total_entries=len(all_items), newest=newest)


def conditional_headers(
    feed: Feed, etag: str | None = None, last_modified: str | None = None
) -> dict[str, str]:
    """Build If-None-Match / If-Modified-Since headers.

    Nothing persists validators today, so in practice this returns `{}` and every request
    goes out unconditional. The 304 *handling* is real and tested; only the storage is
    missing, and deliberately so: the `sources` table has one `etag` column per source while
    this source has six feeds, so wiring it needs a `feed_state(source, feed_name, ...)`
    table. The payoff would be politeness rather than speed -- six small XML files fetched
    once a day -- which does not justify a schema change. Revisit only if a host starts
    rate-limiting (PLAN.md phase 5).
    """
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def staleness_threshold(feed_name: str) -> int:
    return STALENESS_THRESHOLD_DAYS.get(feed_name, DEFAULT_STALENESS_THRESHOLD_DAYS)


def is_stale(feed_name: str, newest: datetime | None, now: datetime) -> bool:
    """A feed whose newest entry predates its threshold. 200 OK, well-formed, and dead."""
    if newest is None:
        return True
    return now - newest > timedelta(days=staleness_threshold(feed_name))


def _describe(outcome: FeedOutcome, now: datetime) -> str:
    """One summary line per feed. The stale case must stay distinguishable from the empty one."""
    name = outcome.feed.name
    if outcome.not_modified:
        return f"{name}: not modified (304)"
    if outcome.total_entries == 0:
        return f"{name}: 0 entries"
    if outcome.newest is None:
        return f"{name}: {len(outcome.items)} items (no dates)"

    age_days = (now - outcome.newest).days
    marker = (
        f" STALE (>{staleness_threshold(name)}d)" if is_stale(name, outcome.newest, now) else ""
    )
    return (
        f"{name}: {len(outcome.items)} recent of {outcome.total_entries}, "
        f"newest {age_days}d old{marker}"
    )


def _item_from_entry(entry: FeedParserDict, source_name: str, feed_name: str) -> Item | None:
    title = entry.get("title")
    link = entry.get("link")
    if not title or not link:
        return None

    raw = dict(entry)
    # Which of the six this came from. Without it, a stale or misbehaving feed is
    # untraceable once its items are mixed into one source's output.
    raw["feed_name"] = feed_name

    return Item(
        url=link,
        title=_WHITESPACE.sub(" ", title).strip(),
        source=source_name,
        author=entry.get("author"),
        published_at=_published_at(entry),
        raw=raw,
    )


def _published_at(entry: FeedParserDict) -> datetime | None:
    """feedparser normalises to a naive UTC struct_time; Item rejects naive datetimes."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)
