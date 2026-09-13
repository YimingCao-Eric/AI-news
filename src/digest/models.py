"""Core domain models.

`Item` is the single normalised shape every adapter maps its source into. Nothing in this
module knows about SQLite, ranking or the network -- see the layering rules in CLAUDE.md.

Note on timestamps: the models carry real `datetime` objects and require them to be
timezone-aware. Conversion to the ISO8601 UTC *strings* that CLAUDE.md mandates for storage
happens at the store boundary (phase 2), not here.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def _require_aware(value: datetime | None, field_name: str) -> datetime | None:
    """Reject naive datetimes instead of assuming they are UTC.

    Sources disagree about timezones: feedparser hands back naive `struct_time`, the HN
    Algolia API returns ISO strings with a `Z`, arXiv's RSS carries its own offset. Silently
    stamping UTC onto a naive value is a multi-hour error that never announces itself, and
    mixing aware and naive values blows up the first time anything sorts by recency. So the
    model refuses naive input and the adapter is made to do the conversion explicitly.
    """
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{field_name} must be timezone-aware; got naive {value!r}. "
            "Convert it in the adapter (e.g. datetime.replace(tzinfo=UTC) for a source "
            "documented as UTC, or zoneinfo for a source with a known local zone)."
        )
    return value


class Item(BaseModel):
    """One normalised news item, from any source.

    The first six fields come from the adapter. `topic`, `score` and `score_reason` are
    filled in by `rank.py`; `summary` by `summarise.py`.

    Store-owned columns (`first_seen_at`, `digest_date`, `feedback` -- see PLAN.md section 4)
    deliberately live only in SQLite, not on this model: adapters have no business
    producing them.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    source: str
    author: str | None
    published_at: datetime | None
    # Deliberately untyped: `raw` is a verbatim passthrough of whatever the source sent,
    # stored as-is so the ranker can be re-run over weeks of history offline after it
    # changes (PLAN.md section 4). Do not narrow it to a per-source TypedDict -- the moment
    # it only holds fields today's ranker reads, re-scoring history stops being possible.
    raw: dict[str, Any]
    topic: str | None = None
    score: float | None = None
    score_reason: str | None = None
    summary: str | None = None

    @field_validator("published_at")
    @classmethod
    def _published_at_is_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value, "published_at")


class SourceHealth(BaseModel):
    """Per-source liveness record, so a feed that dies stops doing so silently.

    Written by the fetch loop and printed as the digest's source-health footer
    (PLAN.md section 2.1).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    last_success_at: datetime | None = None
    consecutive_failures: int = 0

    @field_validator("last_success_at")
    @classmethod
    def _last_success_at_is_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value, "last_success_at")
