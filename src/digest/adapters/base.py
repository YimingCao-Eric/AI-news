"""The adapter contract -- read this before writing the sixth adapter.

An adapter's entire job is: hit one source's endpoint(s) and map the payload to `Item`.
Nothing else. Specifically it must NOT:

* touch the database -- `store.py` owns persistence;
* score, rank, filter by interest, or assign `topic` -- `rank.py` owns that, and it is a
  pure function so that scores can be recomputed over stored history;
* call an LLM -- `summarise.py` is the only module allowed to;
* print or log -- `cli.py` owns every line a human sees, including the mandated
  one-line-per-source run log, so that output stays uniform across sources and adapters
  stay silent in tests. `fetch.py` collects; `cli.py` prints. An adapter with something to
  say hands it back through `drain_notes` below.
* enforce `source.fetch_limit` -- `fetch.py::_apply_fetch_limit` applies it once, to every
  adapter's output, so the cap is a post-condition no adapter can violate by accident.
  Requesting fewer from the server is still welcome as an *optimisation* (`hn` sends
  `hitsPerPage`); it is not the guarantee.

Adapters also do not decide whether they run: `sources.yaml` does, via `Source.enabled`.

**`kind` selects the implementation; `name` identifies the source.** `kind` is required on
every source, validated against `fetch.KNOWN_KINDS` when `sources.yaml` loads, and is the
dispatch key: `fetch_all` looks up the adapter class by `kind` and constructs **one instance
per source**. Today's values are `hn`, `gh_trending`, `hf_papers`, `ai_blogs`, `arxiv` --
one implementation each, which is why they read like source names. A sixth source reusing an
existing implementation declares that existing `kind`, and that is the moment to rename the
implementation rather than subclass it. Set `Adapter.kind` on the class; see below.

**Multi-endpoint sources.** A source is a bundle when `source.feeds is not None` --
**never** infer it from `source.kind`. `kind` says which code runs, not how many endpoints
that code will find: `arxiv` is single-endpoint in the plan and three-endpoint in
`sources.yaml` today, changed by editing config with no adapter change, which is exactly the
property that breaks anything keying shape off `kind`.
`Source._exactly_one_endpoint_shape` guarantees exactly one of `url` / `feeds` is set, and
`source.endpoints` normalises either shape to a list of `Feed`, so an adapter that iterates
`source.endpoints` handles both.

**Errors: raise.** `fetch.py` catches per source, records a `SourceOutcome`, drains notes
and carries on, because one dead feed must never abort a run (CLAUDE.md). An adapter that
swallows its own exceptions defeats that, since the failure never reaches the outcome.

Raise from the taxonomy in `digest.errors` -- `SourcePayloadError` when the response is not
the shape this source is supposed to return, `SourceBlockedError` for a 403 or a rate limit.
What that buys is the 07:00 question: **our bug or their outage?** `fetch.py` classifies
what it caught with `isinstance(exc, AdapterError)`; an anticipated failure reads as the
source being unusable, while a stray `TypeError` from a coding mistake reads as CRASHED.
So do not wrap accidental exceptions in `AdapterError`, and do not raise a bare
`ValueError`/`RuntimeError` for an anticipated failure -- both erase the distinction.
`digest.errors.unmappable_entry` builds the message for a single entry that will not map.

Shared mapping invariants (`Item` rejects naive datetimes; titles are whitespace-normalised)
live in `adapters/_mapping.py`. Use those helpers rather than re-deriving them -- both were
byte-identical copies in two adapters before, where a correction had to be made twice.
"""

from abc import ABC, abstractmethod

import httpx

from digest.config import Source
from digest.models import Item


class Adapter(ABC):
    """Fetches one source and maps it to `Item`s. See the module docstring for the contract."""

    #: Which implementation this is. Matches the `kind` of every source it serves.
    #:
    #: NOT the source's name. This attribute used to hold the source name, which made one
    #: class serve exactly one source: the registry key *was* the class attribute, so a
    #: second source wanting the same behaviour needed a subclass whose only content was a
    #: different name. `kind` is the implementation; `Source.name` is the source; `fetch_all`
    #: constructs one instance per source from the class its `kind` selects.
    kind: str

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, source: Source) -> list[Item]:
        """Fetch `source` and return its items, newest-first where the source allows.

        The client is shared across sources and already carries the User-Agent and timeout;
        adapters must not construct their own.

        On zero items, see CLAUDE.md: **raise when zero means "we no longer understand this
        endpoint", return `[]` when zero is a state the source can genuinely be in.** An
        empty list is indistinguishable from a quiet day, so returning one for a broken
        endpoint is the silent rot the health footer cannot catch.
        """
        raise NotImplementedError

    def drain_notes(self) -> list[str]:
        """Diagnostics from the last fetch, for the run summary. Returns and clears.

        This exists for the state between success and failure that neither `Item` nor
        `SourceOutcome` can express: a bundle source where one feed of six timed out, or
        answered 200 with well-formed XML whose newest entry is four months old. Those must
        be *reported*, not silently absent -- but adapters may not log or print, so they
        hand the strings back and `fetch.py` owns the output.

        Safe as instance state because `fetch_all` constructs one instance per source per
        run -- structurally, not by convention. It previously held instances in a
        process-lifetime registry keyed by source name, where the safety argument was a
        docstring: register one instance under two names and two concurrent coroutines share
        `self._notes`, silently attributing one source's feed diagnostics to the other with
        no exception and no failing test. Per-source construction removes the possibility.
        Adapters with nothing to say inherit this and return nothing.
        """
        return []
