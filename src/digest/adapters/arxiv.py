"""arXiv category RSS. One adapter serves several categories via the source's `feeds`.

The highest-volume source in the project by a wide margin. Measured 2026-09-14: cs.AI 270
entries, cs.CL 115, cs.MA 16 -- five times the ~50/day the plan assumed.
"""

import asyncio
import re

import feedparser
import httpx
from feedparser.util import FeedParserDict

from digest.adapters._mapping import normalise_title, published_at_from_entry
from digest.adapters.base import Adapter
from digest.config import Feed, Source
from digest.errors import SourcePayloadError, unmappable_entry
from digest.models import Item

#: arXiv marks each entry with why it appeared. `new` is a first announcement and `cross` is
#: a first announcement in an additional category; both are new work to a reader.
#: `replace` / `replace-cross` are v2+ revisions of papers that may be months old, and they
#: were 38% of cs.AI on 2026-09-14 (replace-cross 72, replace 31, out of 270). Letting a
#: revision into the digest as if it were fresh is the quiet kind of wrongness that makes
#: you stop trusting the whole thing.
#:
#: Cross-listings that arrive twice through different categories share an arXiv URL, so
#: `store.url_hash` already collapses them -- no extra work needed here.
KEPT_ANNOUNCE_TYPES = frozenset({"new", "cross"})

#: Defensive only. arXiv's *current* RSS carries clean titles -- 0 of 270 cs.AI entries had
#: this prefix, a newline, or a double space when measured on 2026-09-14; the `arXiv:ID
#: Announce Type:` text lives in `description`, not `title`. Older arXiv feed formats did put
#: it in the title, so the strip stays as a guard, tested against a synthetic entry. It is
#: not a hazard present in today's data, and saying otherwise would be inventing one.
_ARXIV_TITLE_PREFIX = re.compile(r"^\s*arXiv:\s*\d{4}\.\d{4,5}(v\d+)?\s*(\[[^\]]*\])?\s*[:\-]?\s*")

#: Per-feed budget. The outer 20s in fetch.py covers the whole source; without this, one slow
#: category could spend it all and cost the other two.
FEED_TIMEOUT_SECONDS = 10.0


class ArxivAdapter(Adapter):
    kind = "arxiv"

    def __init__(self) -> None:
        self._notes: list[str] = []

    def drain_notes(self) -> list[str]:
        notes, self._notes = self._notes, []
        return notes

    async def fetch(self, client: httpx.AsyncClient, source: Source) -> list[Item]:
        self._notes = []
        feeds = source.endpoints

        results = await asyncio.gather(
            *(self._fetch_feed(client, feed, source.name) for feed in feeds),
            return_exceptions=True,
        )

        per_feed: list[list[Item]] = []
        failures = 0
        announced = 0
        for feed, result in zip(feeds, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                failures += 1
                self._notes.append(f"{feed.name}: FAILED {type(result).__name__}: {result}")
                continue
            kept, total_entries = result
            per_feed.append(kept)
            announced += total_entries
            # The pre-filter count is the point: "0 of 0" is a quiet day, "0 of 270" is a
            # broken filter, and without the denominator both print as a bare zero. The
            # filter keys on a string field, so an arXiv rename of `new`/`cross` would drop
            # 100% of input while the source reported success.
            self._notes.append(f"{feed.name}: {len(kept)} new/cross of {total_entries} entries")

        if failures == len(feeds):
            raise SourcePayloadError(
                f"{source.name}: every category failed ({failures}/{len(feeds)}). "
                f"Notes: {'; '.join(self._notes)}"
            )

        if announced == 0 and failures == 0:
            # Every category parsed and every one was empty. arXiv is genuinely quiet on some
            # days, but not on all three categories at once with well-formed documents --
            # CLAUDE.md's zero-items rule: that is "we no longer understand this endpoint".
            raise SourcePayloadError(
                f"{source.name}: all {len(feeds)} categories parsed cleanly and announced "
                f"zero entries between them. One quiet category is normal; all of them is "
                f"the feed shape changing. Re-record the fixtures and check the mapping. "
                f"Notes: {'; '.join(self._notes)}"
            )

        items = _round_robin(per_feed)
        return items

    async def _fetch_feed(
        self, client: httpx.AsyncClient, feed: Feed, source_name: str
    ) -> tuple[list[Item], int]:
        """Return the kept items and how many entries the feed announced before filtering.

        A plain tuple rather than a named per-feed result type: `ai_blogs.FeedOutcome` is one
        already, and a second would be the fourth result type in the project -- the recorded
        trigger for reconciling all of them, which Themes 5 and 6 own. Not pre-empting that
        decision here.
        """
        async with asyncio.timeout(FEED_TIMEOUT_SECONDS):
            response = await client.get(feed.url)
        response.raise_for_status()

        parsed = feedparser.parse(response.text)
        if parsed.bozo and not parsed.entries:
            # Malformed XML *and* nothing parsed: we no longer understand this endpoint.
            raise SourcePayloadError(
                f"{source_name}/{feed.name}: {feed.url} returned unparseable XML "
                f"({parsed.bozo_exception}). Zero entries from a broken document is not a "
                f"quiet day."
            )

        # A genuinely empty category is possible -- arXiv does not announce every day -- so
        # this returns [] rather than raising. The counts reach the summary via notes, and
        # the all-categories-empty case is the caller's to judge.
        kept = [
            item
            for entry in parsed.entries
            if entry.get("arxiv_announce_type", "new") in KEPT_ANNOUNCE_TYPES
            and (item := _item_from_entry(entry, source_name, feed.name)) is not None
        ]
        return kept, len(parsed.entries)


def _round_robin(per_feed: list[list[Item]]) -> list[Item]:
    """Interleave categories rather than concatenating them.

    `fetch_limit` is set high enough to cover everything eligible, so this normally changes
    nothing. It matters on the day the cap *does* bind: concatenated, cs.AI's 270 entries
    would consume the budget and cs.MA's 16 -- the multi-agent category, which is the one
    that actually matches the interest profile -- would never be stored at all. Round-robin
    makes a binding cap fail fairly instead of alphabetically.
    """
    merged: list[Item] = []
    for row in zip(*per_feed, strict=False):
        merged.extend(row)
    # zip() stops at the shortest feed; append whatever the longer ones still hold.
    shortest = min((len(f) for f in per_feed), default=0)
    for feed_items in per_feed:
        merged.extend(feed_items[shortest:])
    return merged


def clean_title(title: str) -> str:
    """Strip the (currently absent) `arXiv:ID` prefix and normalise whitespace."""
    return normalise_title(_ARXIV_TITLE_PREFIX.sub("", title))


def _item_from_entry(entry: FeedParserDict, source_name: str, feed_name: str) -> Item | None:
    title = entry.get("title")
    link = entry.get("link")
    if not title or not link:
        return None

    raw = dict(entry)
    raw["feed_name"] = feed_name

    try:
        return Item(
            url=link,
            title=clean_title(title),
            source=source_name,
            author=entry.get("author"),
            published_at=published_at_from_entry(entry),
            # The abstract stays in `summary` here, untouched. CLAUDE.md forbids fetching or
            # summarising bodies; keeping the one the feed already gave us costs nothing and
            # is what phase 4 will re-score against.
            raw=raw,
        )
    # ValidationError is a ValueError subclass, so this also catches a date string the
    # parser rejects *before* the model sees it -- which is where a malformed entry
    # actually escaped first, naming nothing.
    except ValueError as exc:
        raise unmappable_entry(source_name, feed_name, link, exc) from exc
