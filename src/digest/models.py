"""Core domain models.

`Item` is the single normalised shape every adapter maps its source into. Nothing in this
module knows about SQLite, ranking or the network -- see the layering rules in CLAUDE.md.

Note on timestamps: the models carry real `datetime` objects and require them to be
timezone-aware. Conversion to the ISO8601 UTC *strings* that CLAUDE.md mandates for storage
happens at the store boundary (phase 2), not here.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo


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


class SourceOutcome(BaseModel):
    """What one run did to one source. An **event**, not a state.

    The counterpart to `SourceHealth`, and the reason the two are separate types. One class
    used to serve both directions: the fetch loop wrote it meaning "this run failed" while
    `get_source_health` read it meaning "this source has failed N times", and the store
    recovered the difference by testing `last_success_at is not None`. That inference was
    correct only by accident of how one caller happened to build the object.

    The break it invited: a retry wrapper, or a future `run` command, assembling a
    *complete* record by carrying the previous `last_success_at` forward on a failed run --
    an obviously sensible thing to do, which silently recorded a success and reset the
    failure counter to zero.

    Two things make that unrepresentable here. Exactly one of the timestamps may be set, so
    "failed, but here is when it last worked" cannot be expressed. And there are **no
    counts** on this type at all: `consecutive_failures` and `total_failures` are computed
    by `store.record_source_health` from the stored row, because the caller cannot see the
    previous value. Removing the field is what removes the temptation.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    #: When this run succeeded. None means it did not.
    succeeded_at: datetime | None = None
    #: When this run failed. None means it did not.
    failed_at: datetime | None = None
    #: The failure, as text. Only meaningful alongside `failed_at`.
    error: str | None = None
    #: True when the adapter *anticipated* the failure and raised a named `AdapterError`;
    #: False when it crashed with something nobody planned for.
    #:
    #: This is the our-bug-versus-their-outage distinction, carried from `fetch.py`'s catch
    #: site to the run log and the footer. Deliberately NOT persisted to the `sources` table:
    #: "which runs crashed" is run provenance, which is SF-4/SF-5 and deferred against phase
    #: 4's runs table. Visible in the run that produced it, not stored.
    expected_failure: bool = False

    @field_validator("succeeded_at", "failed_at")
    @classmethod
    def _instant_is_aware(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        return _require_aware(value, str(info.field_name))

    @model_validator(mode="after")
    def _exactly_one_instant(self) -> "SourceOutcome":
        if self.succeeded_at is not None and (self.error or self.expected_failure):
            raise ValueError(f"source {self.name!r}: a successful run has no error to classify.")
        if (self.succeeded_at is None) == (self.failed_at is None):
            both = self.succeeded_at is not None
            raise ValueError(
                f"source {self.name!r}: a run either succeeded or failed, so set exactly one "
                f"of succeeded_at / failed_at -- got "
                f"{'both' if both else 'neither'}. If you are carrying a previous success "
                f"forward onto a failed run, do not: the store preserves last_success_at "
                f"across failures, so there is nothing to carry."
            )
        return self

    @property
    def succeeded(self) -> bool:
        """Direct read, not an inference -- the validator guarantees exactly one instant."""
        return self.succeeded_at is not None


class SourceHealth(BaseModel):
    """Per-source liveness **state**, accumulated across runs.

    Read back from the `sources` table by `store.get_source_health` and printed as the
    digest's source-health footer (PLAN.md section 2.1). Never written by the fetch loop --
    that produces a `SourceOutcome`.

    `total_failures` and `last_failure_at` exist because `consecutive_failures` answers "is
    it failing right now", not "has it been failing". A source that fails on odd days and
    succeeds on even ones read as perfectly healthy every time you looked after a success:
    the counter reset and nothing else remembered.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    #: Never cleared by a success. The half that survives recovery.
    last_failure_at: datetime | None = None
    total_failures: int = 0

    @field_validator("last_success_at", "last_failure_at")
    @classmethod
    def _timestamps_are_aware(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        return _require_aware(value, str(info.field_name))
