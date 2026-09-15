"""Hugging Face daily papers.

The curated list -- roughly 50 papers a day that a human submitted and others upvoted --
rather than the raw model or paper firehose. That curation is the whole reason this source
outranks arXiv when the same paper appears in both (see `tests/test_cross_source_dupes.py`).
"""

from datetime import datetime
from typing import Any

import httpx

from digest.adapters._mapping import normalise_title, parse_iso_utc
from digest.adapters.base import Adapter
from digest.config import Source
from digest.errors import SourcePayloadError, unmappable_entry
from digest.models import Item

PAPER_URL = "https://huggingface.co/papers/{paper_id}"


class HFPapersAdapter(Adapter):
    kind = "hf_papers"

    async def fetch(self, client: httpx.AsyncClient, source: Source) -> list[Item]:
        if source.url is None:  # pragma: no cover -- the config validator forbids it
            raise ValueError(f"source {source.name!r} has no url")

        response = await client.get(source.url)
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, list):
            raise SourcePayloadError(
                f"{source.name}: expected a JSON list from {source.url}, got "
                f"{type(payload).__name__}. The API shape has changed."
            )
        if not payload:
            # CLAUDE.md zero-items rule: raise. daily_papers is a fixed curated list that is
            # never empty; zero means the endpoint changed, and returning [] would be
            # indistinguishable from a quiet day forever after.
            raise SourcePayloadError(
                f"{source.name}: {source.url} returned 200 with zero papers. This endpoint "
                f"publishes ~50 every day, so zero means its shape changed, not that the "
                f"day was quiet. Re-record the fixture and check the mapping."
            )

        items = [item for entry in payload if (item := _item_from_entry(entry, source.name))]
        return items


def _item_from_entry(entry: dict[str, Any], source_name: str) -> Item | None:
    paper = entry.get("paper") or {}
    paper_id = paper.get("id")
    title = paper.get("title") or entry.get("title")
    if not paper_id or not title:
        return None

    url = PAPER_URL.format(paper_id=paper_id)
    try:
        return Item(
            url=url,
            title=normalise_title(title),
            source=source_name,
            author=_submitter(entry),
            published_at=_published_at(entry),
            # The whole entry, unedited. `paper.summary` is a full abstract (~1.1 KB) and
            # belongs here rather than in a truncated field: CLAUDE.md forbids summarising
            # article bodies, and PLAN.md section 4 wants the payload intact so phase 4 can
            # re-score history.
            raw=entry,
        )
    # ValidationError is a ValueError subclass, so this also catches a date string the
    # parser rejects *before* the model sees it -- which is where a malformed entry
    # actually escaped first, naming nothing.
    except ValueError as exc:
        raise unmappable_entry(source_name, None, url, exc) from exc


def _submitter(entry: dict[str, Any]) -> str | None:
    """Who put it on the daily list -- not the paper's authors, who are in `paper.authors`."""
    submitted_by = entry.get("submittedBy")
    if isinstance(submitted_by, dict):
        return submitted_by.get("name") or submitted_by.get("fullname")
    return submitted_by if isinstance(submitted_by, str) else None


def _published_at(entry: dict[str, Any]) -> datetime | None:
    """`publishedAt` first, `submittedOnDailyAt` as a fallback.

    The fallback is for an *absent* field, not a malformed one: the first key that is present
    is the one that must parse, so a shape change is reported rather than silently skipped
    past to the second-best field.
    """
    for key in ("publishedAt", "submittedOnDailyAt"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return parse_iso_utc(value)
    return None
