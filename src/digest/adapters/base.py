"""The adapter contract.

An adapter's entire job is: hit one source's endpoint(s) and map the payload to `Item`.
Nothing else. Specifically it must NOT:

* touch the database -- `store.py` owns persistence;
* score, rank, filter by interest, or assign `topic` -- `rank.py` owns that, and it is a
  pure function so that scores can be recomputed over stored history;
* call an LLM -- `summarise.py` is the only module allowed to;
* print or log -- the caller (`fetch.py`) owns the one-line-per-source run log, so that
  output stays uniform across sources and adapters stay silent in tests.

Adapters also do not decide whether they run: `sources.yaml` does, via `Source.enabled`.

**Multi-endpoint sources.** A source is a bundle when `source.feeds is not None` -- never
test `source.kind`, which is free-form text that happens to read `rss` for both the
single-endpoint `arxiv_cs_ai` and the six-endpoint `ai_blogs`. `Source._exactly_one_endpoint_shape`
guarantees exactly one of `url` / `feeds` is set, and `source.endpoints` normalises either
shape to a list of `Feed`, so an adapter that iterates `source.endpoints` handles both.

Errors: raise. `fetch.py` catches per source, records the failure in `SourceHealth` and
carries on, because one dead feed must never abort a run (CLAUDE.md). An adapter that
swallows its own exceptions defeats that, since the failure never reaches the health record.
"""

from abc import ABC, abstractmethod

import httpx

from digest.config import Source
from digest.models import Item


class Adapter(ABC):
    """Fetches one source and maps it to `Item`s. See the module docstring for the contract."""

    #: Must match the `name` of the source in sources.yaml this adapter serves.
    name: str

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, source: Source) -> list[Item]:
        """Fetch `source` and return its items, newest-first where the source allows.

        The client is shared across sources and already carries the User-Agent and timeout;
        adapters must not construct their own.
        """
        raise NotImplementedError
