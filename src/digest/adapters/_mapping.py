"""Shared entry-to-`Item` helpers, used by every adapter.

Small on purpose. These are the three operations that were duplicated byte-for-byte across
adapters, not an attempt to unify the adapters themselves -- the bundle fan-out in `arxiv`
and `ai_blogs` stays duplicated until a third RSS bundle source makes the right hook
signature knowable (DUP-1/DUP-5, deferred).

What they have in common is that each enforces an invariant the project has held since phase
0, and each was enforced in more than one place. A correction to the datetime rule had to be
made twice, and whichever copy was missed would fail for one source only -- reading as "that
feed is malformed" rather than "we fixed this in the wrong file".
"""

import re
from datetime import UTC, datetime

from feedparser.util import FeedParserDict

_WHITESPACE = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Collapse runs of whitespace and strip.

    A guard, not a repair: zero of the 33 recorded HN titles and zero of the 2 000-odd
    recorded RSS titles contain a tab, a newline or a double space. It is here because
    `cli` renders each item as two aligned lines and phase 3c renders Markdown, both of which
    a newline inside a title would break -- and because four of five adapters had already
    independently decided this was needed, which is the codebase answering the question.
    """
    return _WHITESPACE.sub(" ", title).strip()


def published_at_from_entry(entry: FeedParserDict) -> datetime | None:
    """feedparser's `struct_time` -> aware UTC.

    feedparser normalises every date it can parse to UTC and hands back a *naive*
    `struct_time`, so the tzinfo has to be reattached. `models.Item` rejects naive datetimes,
    and the tempting fix when that fires -- relaxing the validator -- would undo phase 0.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def parse_iso_utc(value: str | None) -> datetime | None:
    """An ISO 8601 string from a JSON API -> aware UTC. None only for an *absent* value.

    Handles the `Z` suffix that `fromisoformat` accepts only from 3.11, and re-stamps UTC on
    a value that carried no offset at all.

    **Raises on a present-but-unparseable string rather than returning None.** Absent and
    malformed are different: absent means the source did not say, and an item with no date is
    a state `Item` supports. Malformed means the source said something we no longer
    understand, and quietly turning that into "no date" would strip the item's recency
    silently -- the item survives, so nothing looks dropped, and it simply never ranks. The
    caller wraps this in `unmappable_entry`, which names the offending entry.

    Extracting this nearly introduced exactly that degradation: the first version returned
    None on a parse failure so a caller could try a second field, and a test asserting a bad
    datum fails loudly caught it.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
