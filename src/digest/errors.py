"""The project's exception taxonomy, in one place because it was in five.

Before this module, seven deliberate raises across the adapters used `ValueError`,
`TypeError`, `RuntimeError`, `LookupError` and one bespoke `GitHubTrendingError`, all
flattened by `fetch.py` into `f"{type(exc).__name__}: {exc}"`. The distinction that was lost
is the one you most need at 07:00: **is this our bug or their outage?** A deliberate
`TypeError` meaning "the API returned a dict, not a list" and an accidental one from a coding
mistake produced the same health record, the same footer line and the same log shape. The
first means re-record a fixture; the second means revert a commit.

Two axes, deliberately separate:

* `AdapterError` — a source cannot be used, and the adapter **anticipated** it. Raised on
  purpose. Accidental `TypeError`/`AttributeError` stay whatever Python raised, because the
  whole value of the base is telling the two apart. `fetch.py` classifies with `isinstance`
  at its catch site; it does **not** narrow the catch. Narrowing would let a coding mistake
  bypass the health record and the notes drain entirely.

* `DigestControlError` — the program's assumptions about its own *execution* are violated.
  Outside `Exception` on purpose, so it punches through that same catch-all instead of being
  filed as a dead feed.

**The rest of the taxonomy lives elsewhere and is not moved by this module.** Two exceptions
predate it and stay where they are raised:

* `digest.store.StoreError` — schema mismatch, unusable or missing database, naive timestamp.
* `digest.config.ConfigError` — missing or malformed `sources.yaml` / `interests.yaml`.

Consolidating those here would unify the taxonomy but is a rename outside the theme that
introduced this file; the pointer is here so "one place to read the taxonomy" is true today
rather than aspirational. See docs/reviews/found-during-r3.md.
"""


class DigestControlError(BaseException):
    """The program's own execution is broken -- not a data source misbehaving.

    **The test for this base:** an assumption the program makes about how it is running has
    been violated. A test reached the network when the suite guarantees it cannot; a fixture
    route is missing from a harness that is supposed to cover every request. None of these
    are things a feed did.

    Derives from `BaseException` rather than `Exception` so that it survives
    `fetch.py`'s deliberately broad `except Exception`. That catch exists so no source can
    abort a run, which is correct -- and it means anything inside `Exception` raised from a
    harness gets recorded as "that source failed" and the run carries on with a quietly
    wrong result. Both subclasses below were discovered by a test failing with the wrong
    message, which is two discoveries too many; hence a shared, findable base.

    In production this should never be raised. `cli.main` maps it to exit 70 (EX_SOFTWARE)
    and logs the traceback: distinguishable from 4 (every source failed), because 4 means
    "the environment failed, tomorrow may work" and this means "this is a bug, retrying
    cannot help".
    """


class AdapterError(RuntimeError):
    """A source cannot be used, and the adapter said so deliberately.

    Anticipated failures only: a 403, selectors that no longer match, a payload of the wrong
    shape, every feed in a bundle empty. Never used to wrap an accidental exception -- a
    `TypeError` from a coding mistake must stay a `TypeError`, because `fetch.py` tells the
    two apart by `isinstance` and reports them differently.

    Inherits `RuntimeError` so pre-existing `except Exception` handlers are unaffected.
    Checked before choosing: nothing in `src/`, `tests/` or `scripts/` catches `RuntimeError`
    or uses a bare `except`, so this widens no existing handler.
    """


class SourcePayloadError(AdapterError):
    """The response does not look like what this source is supposed to return.

    Wrong JSON type, unparseable XML, selectors matching nothing, zero items where zero is
    not a state the source can genuinely be in, an entry that cannot be mapped to an `Item`.
    The 07:00 response is: re-record the fixture and check the mapping.
    """


class SourceBlockedError(AdapterError):
    """The host is deliberately refusing us -- 403, or a rate limit.

    Distinct from `SourcePayloadError` because the response differs: check the User-Agent,
    back off, do not retry in a tight loop. Nothing about the mapping is wrong.
    """


class NoAdapterRegistered(AdapterError):
    """An enabled source in sources.yaml has no adapter registered for it.

    Anticipated and deliberate -- a real misconfiguration rather than a crash -- but it is
    still "this source cannot be used", so it belongs in the same hierarchy rather than
    being a bare `LookupError` nobody can grep for.
    """


def unmappable_entry(
    source_name: str, feed_name: str | None, identifier: str, cause: Exception
) -> SourcePayloadError:
    """Build the error for one entry that cannot be turned into an `Item`.

    The finding this answers: `Item` is constructed inside comprehensions over as many as 270
    entries, so a single unparseable date failed a whole feed with a message naming neither
    the entry nor its URL -- you could not tell which of 270 to look at, and if the bad entry
    came from a live feed rather than a fixture it was gone by the time you looked.

    The whole feed still fails, deliberately. Skipping the bad entry and counting it would be
    kinder, but three of the five adapters have no `drain_notes` channel to report the count
    through, so the skip would be visible in two places and silent in three -- a silent drop
    in exactly the spots nobody can see. That trade flips once DUP-5 gives every adapter a
    notes channel; until then, loud and disproportionate beats quiet and proportionate.
    See docs/reviews/found-during-r3.md.
    """
    where = f"{source_name}/{feed_name}" if feed_name else source_name
    return SourcePayloadError(
        f"{where}: cannot map entry {identifier} to an Item -- {cause}. "
        f"One malformed entry fails this feed rather than being dropped silently; "
        f"the identifier above is the one to look at."
    )
