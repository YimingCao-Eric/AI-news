# Review: duplication and the adapter shape

Interphase review, dimension 3. Reviewed against `CLAUDE.md` and `docs/PLAN.md` at commit
`23d0b45` (five adapters). **No files were changed.**

Scope: what is genuinely repeated across the five adapters, what belongs in `base.py` or a
shared helper, and what only looks shared. Each extraction is costed, because five honest
copies beat one premature abstraction.

**Headline: the duplication is not spread across five adapters — it is concentrated between
two.** `arxiv.py` and `ai_blogs.py` share roughly 45 lines of bundle fan-out, per-feed
timeout, partial-success handling and feedparser mapping. `hn.py`, `hf_papers.py` and
`gh_trending.py` share almost nothing with each other beyond one-line idioms that are not
worth extracting. Any extraction that tries to cover all five will be the premature
abstraction you are warning about; the correct extraction covers two, about to be three.

Six findings, then an explicit list of what only looks shared.

---

## 1. Bundle fan-out and partial-success handling exist twice, and have already drifted

**`src/digest/arxiv.py:54-84`** vs **`src/digest/adapters/ai_blogs.py:100-133`** — and
**`arxiv.py:86-100`** vs **`ai_blogs.py:134-155`**.

**What is wrong.** Both adapters independently implement: reset notes, `asyncio.gather(...,
return_exceptions=True)` over `source.endpoints`, a `zip(..., strict=True)` loop that re-raises
non-`Exception` `BaseException` and records everything else as a per-feed `FAILED` note, an
`if failures == len(feeds): raise` check, and a `fetch_limit` slice. The per-feed helpers then
both do `asyncio.timeout(...)` → `raise_for_status()` → `feedparser.parse()` → `if parsed.bozo
and not parsed.entries: raise`. Structurally these are the same eighteen and six lines twice.

**Why it matters.** Two consequences, one already realised.

*Already realised:* the copies have drifted where nothing forced them to agree. The same
condition produces `"arxiv_cs_ai: every category failed (3/3)"` (`arxiv.py:76`) and
`"ai_blogs: all 6 feeds failed"` (`ai_blogs.py:128`). Grepping a month of logs for bundle
failures needs two patterns, and neither message is wrong, which is why nobody noticed.

*Not yet realised:* the observability review found that `failures == len(feeds)` counts only
*exceptions*, so a bundle where every feed parses fine and yields zero items is reported as a
success. That defect is present in **both** copies. Fixing it is two edits in two files with
no test linking them — precisely the shape where a fix lands in one and not the other. The
sixth adapter (PLAN §2 Tier 2 is mostly RSS: `releases.atom` for eleven repos, Reddit,
Lobsters) makes it three copies.

**Smallest fix.** A `BundleAdapter` in `base.py` owning fan-out, timeout, partial success,
notes and the all-failed check, with one abstract hook per feed.

**Cost, honestly.** This is the expensive extraction and it is not free. `arxiv._fetch_feed`
returns `list[Item]`; `ai_blogs._fetch_feed` returns a `FeedOutcome` carrying
`total_entries`/`newest`/`not_modified`, because staleness must be measured before the age
cutoff. Unifying them means either arxiv gains a wrapper object it has no use for, or the hook
returns `(items, note_text)` and ai_blogs computes its note inside the hook — which is the
cheaper shape and the one I would take. Either way you add an inheritance level to a codebase
that currently has one flat ABC, and `base.py` grows from 68 lines to perhaps 130.

**Recommendation on timing:** do this *when the sixth RSS source lands*, not before. At two
copies the drift is annoying; at three it is a maintenance bug. But extract finding 2 now —
it is the part with no cost.

**Bucket: A.**

---

## 2. `_published_at` for feedparser is byte-identical in two files

**`src/digest/adapters/arxiv.py:158-163`** and **`src/digest/adapters/ai_blogs.py:252-257`** —
same body, same fallback order (`published_parsed or updated_parsed`), same
`datetime(*parsed[:6], tzinfo=UTC)`. Only the docstring wording differs.
`_item_from_entry` (`arxiv.py:136-155`, `ai_blogs.py:231-249`) is near-identical too: same
guard, same `raw = dict(entry); raw["feed_name"] = feed_name`, same five `Item` fields —
diverging only at `clean_title(title)` vs `_WHITESPACE.sub(" ", title).strip()`.

**What is wrong.** A feedparser-specific conversion, duplicated.

**Why it matters.** This is the code that satisfies the phase-0 rule that `Item` rejects naive
datetimes — the project's oldest invariant. Any correction to it must be made twice: a
feedparser version that starts returning tz-aware structs, a decision to prefer `updated_parsed`
over `published_parsed`, or handling a feed that supplies `dc:date` instead. One of the two
will be missed, and the symptom is a `ValidationError` from one source only, which reads as
"that feed is malformed" rather than "we fixed this in the wrong file".

**Smallest fix.** Move both into `adapters/_rss.py` (or `base.py`) as
`published_at_from_entry(entry)` and `item_from_entry(entry, source_name, feed_name, title)`,
with the title already normalised by the caller. Two plain module-level functions.

**Cost.** Near zero. No inheritance, no hook, no type unification, no behaviour change — the
two implementations are already identical, so this is deleting one copy rather than inventing
an abstraction. This is the extraction that is obviously correct today.

**Bucket: A.**

---

## 3. HN titles are the only ones not whitespace-normalised

**`src/digest/adapters/hn.py:104`** — `title=title,` straight from the payload.
Compare `hf_papers.py:64`, `ai_blogs.py:244`, `arxiv.py:147`, all of which normalise.

**What is wrong.** Four of five adapters collapse runs of whitespace and strip; the fifth does
not. Not duplication — its absence — but it is the same question: is title normalisation a
shared concern or a per-source one? Four adapters have already answered "shared" by
independently implementing it.

**Why it matters.** `cli.py:123-124` renders each item as two lines, title then URL, indented
to align. An HN title containing a newline — Algolia passes through what the submitter typed —
breaks that alignment, and in phase 3c will break the Markdown template the same way. It also
lands unnormalised in `raw_json` *and* in the committed pipeline snapshot, so the snapshot
would record the ragged version as correct.

**Smallest fix.** One call at `hn.py:104`. If finding 2 lands, it becomes the same
`normalise_title` the other four use.

**Bucket: A.**

---

## 4. The `fetch_limit` slice is copied four times and means something different in the fifth

**`hf_papers.py:50-51`**, **`arxiv.py:82-83`**, **`ai_blogs.py:132-133`**,
**`gh_trending.py:61-62`** — all `if source.fetch_limit is not None: items = items[:
source.fetch_limit]`. **`hn.py:88-89`** instead sets `params["hitsPerPage"]`.

**What is wrong.** Four identical two-line blocks, plus one adapter where the same config word
controls a request parameter rather than a truncation.

**Why it matters.** The duplication itself is cheap; the inconsistency is not. Because `hn`
never slices, its post-condition differs: if Algolia ever returns more than `hitsPerPage`, or
the parameter is dropped in a URL edit, `hn` silently exceeds its configured limit while the
others cannot. Four copies also mean four places to check when asking "does `fetch_limit`
apply before or after the round-robin?" — a question `arxiv.py:115` had to answer in a comment.

**Smallest fix.** Apply the cap once in `fetch.py` after the adapter returns, and delete it
from all four. `hn` keeps `hitsPerPage` as a request *hint* and gains the same post-condition
for free.

**Cost.** Adapters lose the ability to cap before doing per-item work. None of them do any —
mapping is already complete before the slice in all four cases — and arxiv's round-robin
ordering survives because the cap becomes a slice of the list it already returns. The one real
loss is that an adapter can no longer use `fetch_limit` to avoid work it has not yet done,
which matters only for a future paginating source; that source can read `source.fetch_limit`
itself, as `hn` already does.

**Bucket: A.**

---

## 5. `__init__` and `drain_notes` are the same six lines in both bundle adapters

**`src/digest/adapters/arxiv.py:47-52`** and **`src/digest/adapters/ai_blogs.py:95-100`** —
identical: `self._notes: list[str] = []`, then
`notes, self._notes = self._notes, []; return notes`.

**What is wrong.** The `drain_notes` protocol is declared in `base.py:54` with a default
returning `[]`, but every adapter that actually has notes must re-implement the same storage
and the same drain.

**Why it matters.** The layering review found that this state is held on a process-lifetime
singleton and is safe only by an invariant. Two copies means two places to change when that
invariant is made structural rather than documented — and a sixth adapter author will copy
whichever of the two they happen to open, with no signal that they are copying rather than
inheriting.

**Smallest fix.** Give `base.Adapter` a concrete `_notes: list[str]` initialised in `__init__`
and a concrete `drain_notes` that drains it, leaving subclasses to only `append`. Six lines
deleted from two files, no behaviour change.

**Cost.** Near zero, with one caveat: `Adapter` currently has no `__init__`, so adding one
means subclasses that define their own must call `super().__init__()`. `gh_trending`,
`hf_papers` and `hn` define none, so only the two bundle adapters change — and they would lose
theirs entirely.

**Bucket: A.**

---

## 6. ISO-with-`Z` parsing is duplicated between the two JSON adapters

**`src/digest/adapters/hn.py:130-132`** and **`src/digest/adapters/hf_papers.py:91-94`** —
both do `datetime.fromisoformat(value.replace("Z", "+00:00"))` followed by
`parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)`.

**What is wrong.** The same two-line idiom for "JSON API ISO string → aware datetime" in two
files.

**Why it matters.** Less than findings 1–5. The surrounding logic genuinely differs: `hn`
prefers the integer `created_at_i` and falls back to the string; `hf_papers` loops over
`publishedAt` then `submittedOnDailyAt`. Only the innermost conversion is shared. The
consequence is narrow — a future source using a different ISO dialect (an offset without a
colon, say) gets fixed in one place — but it is the same invariant as finding 2, so it belongs
in the same helper if that one lands.

**Smallest fix.** `parse_iso_utc(value: str) -> datetime | None` beside the feedparser helper
from finding 2. Two call sites.

**Cost.** Near zero, but only worth doing *as part of* finding 2 — on its own it trades two
inline lines for an import, which is a wash.

**Bucket: A.**

---

## What only looks shared

Named explicitly, because extracting these would be the mistake.

**`_WHITESPACE = re.compile(r"\s+")` in four files** (`hf_papers.py:20`, `arxiv.py:37`,
`ai_blogs.py:57`, `gh_trending.py:34`). It looks like four copies of one thing. It is not: three
use it to normalise a *title*, `gh_trending.py:128-131` uses it inside `_text()` to collapse
whitespace in scraped DOM text, which is a different job that happens to need the same regex.
Extracting the regex couples four files to a shared module to save `re.compile(r"\s+")`.
Extracting `normalise_title` — the three title sites — is worthwhile, and is exactly what
finding 2 subsumes. Extract the concept, not the pattern.

**Age cutoffs.** Only `ai_blogs.py:55` has one (`MAX_ENTRY_AGE_DAYS = 30`). `hn.py:35`'s
`LOOKBACK_HOURS = 48` looks like the same concept and is not: it parameterises the outgoing
*request* so old items are never fetched, whereas ai_blogs filters *after* parsing because RSS
gives you the whole back catalogue whether you want it or not. `arxiv`, `hf_papers` and
`gh_trending` need neither, because those endpoints publish today's list. A shared
"age cutoff" abstraction over one real user and one false cognate would be the textbook
premature extraction.

**`FEED_TIMEOUT_SECONDS`** — 10.0 in `arxiv.py:41`, 8.0 in `ai_blogs.py:33`. Same name, same
purpose, different values, each justified locally by feed count against `fetch.py`'s 20s
whole-source budget. If finding 1 lands, this becomes a class attribute with a default and two
overrides; on its own, unifying the values would be a behaviour change dressed as a cleanup.

**`response.raise_for_status()`** — in all five. One line, no shared handling to hoist, and
`gh_trending.py:49-57` deliberately checks 403 *before* it. Leave it.

**User agents.** Zero duplication: set once at `fetch.py:138`, and `base.py:44-45` explicitly
forbids adapters from constructing their own client. This is the dimension's cleanest area and
the model the rest could follow — one owner, stated in the contract.

**Error handling.** Also clean. Every adapter raises and `fetch.py:101` catches; no adapter
swallows its own exceptions. The per-adapter *decisions* differ (`gh_trending` raises on zero
rows, `hn` returns `[]`) but that is the zero-items rule in CLAUDE.md working as designed, not
inconsistency.

---

## Buckets

Six findings, all **A**. **No B again** — and since that is now three dimensions running, the
pattern deserves a note rather than another bare assertion.

The candidate worth testing here is **"no new third-party dependency without asking"**. There
is a weak causal link: a rule against dependencies means shared machinery must be hand-built,
and hand-built machinery gets copy-pasted before it gets extracted — which is exactly the
arxiv/ai_blogs story. But it does not survive scrutiny as harm. No library would remove this
duplication: `feedparser` is already in use and handles the parsing; the duplicated part is
~30 lines of `asyncio.gather` fan-out that no sensible dependency would justify. The fix is
available inside the rules, it is just work nobody has done yet. That is an omission, not a
constraint cost.

**No C findings either.** Nothing here is a case where the obvious improvement would be a
regression — the closest is finding 4, where moving the cap to `fetch.py` could be argued to
take a decision away from adapters, but no current adapter uses it for anything a caller could
not do.

### If you take only one thing

Finding 2, and only finding 2. It deletes a byte-identical copy at zero design cost. Findings
1 and 5 are correct but should wait for the sixth RSS source to make them cheap — extracting a
`BundleAdapter` over two implementations means guessing at the hook signature, and the third
implementation is what tells you whether the guess was right.
