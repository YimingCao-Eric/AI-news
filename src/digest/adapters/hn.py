"""Hacker News, via the Algolia `search_by_date` endpoint.

`search_by_date` rather than `tags=front_page`: the front page is a snapshot of whatever
happens to be there at the instant of the call, so a job that runs once each morning misses
anything that peaked overnight and rolled off. Date search with a points floor gets the same
stories whenever they crossed the threshold.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

import httpx

from digest.adapters._mapping import normalise_title, parse_iso_utc
from digest.adapters.base import Adapter
from digest.config import Source
from digest.errors import unmappable_entry
from digest.models import Item

#: Placeholder in the configured URL's query string, replaced with a unix timestamp.
SINCE_TS_PLACEHOLDER = "{since_ts}"

#: How far back the outgoing *request* asks for: items older than this are never fetched.
#:
#: One of three windows that must stay distinct, now named for the stage each acts at:
#:   REQUEST_WINDOW_HOURS (here)        -- the request; old items never arrive
#:   ai_blogs.INGEST_MAX_AGE_DAYS       -- after parsing; old entries arrive but are not stored
#:   interests.yaml max_age_hours       -- selection; how old an item may be and still be chosen
#:
#: They share the value 48 today and are NOT the same concept.
#:
#: 48h, not 24h: a story posted 30 hours ago that only crosses 100 points this morning was
#: never inside a 24h window on either run, so slow burners would be missed permanently.
#: Re-seeing yesterday's items costs nothing once phase 2's url-hash dedupe lands.
#:
#: This equals `max_age_hours` in interests.yaml today, but they are NOT the same concern and
#: must not be merged: this is how far back we ingest, `max_age_hours` is how old an item may
#: be and still be selected into a digest. They are meant to be able to diverge -- fetching
#: 72h while selecting 48h is a reasonable thing to want once slow-burner behaviour is
#: understood. The right move is an assertion that this is >= max_age_hours, never a merge;
#: collapsing them silently undoes the slow-burner fix above.
#:
#: **Owed by phase 3b (LC-6, deferred in docs/reviews/triage.md).** Written as "the right
#: phase 3 move" while phase 3 was ahead of us; 3a shipped without it, so the instruction had
#: quietly become a description of something that already should have happened. The assertion
#: belongs wherever `max_age_hours` is first read for selection.
REQUEST_WINDOW_HOURS = 48

#: Any leftover `{...}` after substitution means a typo'd placeholder in sources.yaml.
_UNRESOLVED_PLACEHOLDER = re.compile(r"\{[^}]*\}")

_HN_ITEM_URL = "https://news.ycombinator.com/item?id={object_id}"


class HNAdapter(Adapter):
    kind = "hn"

    async def fetch(self, client: httpx.AsyncClient, source: Source) -> list[Item]:
        base_url, params = build_request(source, now=datetime.now(tz=UTC))
        response = await client.get(base_url, params=params)
        response.raise_for_status()
        hits = response.json().get("hits", [])
        return [item for hit in hits if (item := _item_from_hit(hit, source.name)) is not None]


def build_request(source: Source, now: datetime) -> tuple[str, dict[str, Any]]:
    """Split the configured URL into a base and a params dict, resolving `{since_ts}`.

    The query string is parsed apart and handed to httpx as `params=` rather than being
    substituted into a URL string, because `numericFilters` contains `>` and `,` which must
    be percent-encoded. Algolia currently tolerates them raw; that is not a property to
    depend on. The placeholder is replaced inside an already-parsed *param value*, so the
    URL text itself is never patched.

    Which param carries the placeholder is discovered, not hardcoded -- move the window to a
    different Algolia param in sources.yaml and this keeps working.
    """
    if source.url is None:  # pragma: no cover -- the config validator forbids it
        raise ValueError(f"source {source.name!r} has no url; HN is not a bundle source.")

    base_url, _, query = source.url.partition("?")
    since_ts = int((now - timedelta(hours=REQUEST_WINDOW_HOURS)).timestamp())

    params: dict[str, Any] = {
        key: value.replace(SINCE_TS_PLACEHOLDER, str(since_ts))
        for key, value in parse_qsl(query, keep_blank_values=True)
    }

    # A typo'd placeholder (`{since-ts}`) would otherwise ship a literal brace to Algolia,
    # and whether that 400s or silently returns unfiltered results is Algolia's parser's
    # decision, not ours. Fail here instead.
    for key, value in params.items():
        if leftover := _UNRESOLVED_PLACEHOLDER.search(str(value)):
            raise ValueError(
                f"source {source.name!r}: query param {key!r} still contains the unresolved "
                f"placeholder {leftover.group()!r} after substitution. Known placeholders: "
                f"{SINCE_TS_PLACEHOLDER}. Check sources.yaml for a typo."
            )

    if source.fetch_limit is not None:
        params["hitsPerPage"] = source.fetch_limit
    return base_url, params


def _item_from_hit(hit: dict[str, Any], source_name: str) -> Item | None:
    """Map one Algolia hit to an `Item`, or None if it is not a usable story."""
    title = hit.get("title")
    object_id = hit.get("objectID")
    if not title or not object_id:
        return None

    url = hit.get("url") or _HN_ITEM_URL.format(object_id=object_id)
    try:
        return Item(
            # Ask HN / Show HN and other text posts have no outbound url; the discussion
            # thread is the artefact in that case.
            url=url,
            title=normalise_title(title),
            source=source_name,
            author=hit.get("author"),
            published_at=_published_at(hit),
            # The whole hit, unedited -- `_highlightResult` included. PLAN.md section 4
            # keeps the original payload so phase 4 can re-score weeks of history after the
            # ranker changes, and the field you did not think you needed is the one you will
            # want then. "raw is raw" is worth more than the kilobytes.
            raw=hit,
        )
    # ValidationError is a ValueError subclass, so this also catches a date string the
    # parser rejects *before* the model sees it -- which is where a malformed entry
    # actually escaped first, naming nothing.
    except ValueError as exc:
        raise unmappable_entry(source_name, None, url, exc) from exc


def _published_at(hit: dict[str, Any]) -> datetime | None:
    """Prefer the integer `created_at_i` over the ISO string.

    Both yield a tz-aware UTC datetime, but the integer cannot be misparsed, so awareness is
    structural rather than dependent on a `Z` suffix surviving. It is also the same field
    `numericFilters` filters on, which removes any skew between what we asked for and what
    we stored.
    """
    created_at_i = hit.get("created_at_i")
    if isinstance(created_at_i, int | float):
        return datetime.fromtimestamp(created_at_i, tz=UTC)
    return parse_iso_utc(hit.get("created_at") or "")
