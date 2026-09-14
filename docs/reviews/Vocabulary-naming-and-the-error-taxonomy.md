# Review: vocabulary, naming and the error taxonomy

Interphase review, dimension 5. Reviewed against `CLAUDE.md` and `docs/PLAN.md` at commit
`23d0b45`. **No files were changed.**

Two questions, answered separately below: does the vocabulary hold together for a stranger, and
is the exception taxonomy coherent enough to diagnose a 07:00 failure from a log alone.

## Would a stranger predict these?

| term | what a stranger guesses | what it means | verdict |
|---|---|---|---|
| `fetch_limit` | cap on items pulled | exactly that | ✅ clear — the `quota` distinction is documented at `sources.yaml:4-7` and on the field itself |
| `quota` | cap on items shown | exactly that | ✅ clear |
| `stale_after_days` | days before a feed counts as stale | exactly that | ✅ clear |
| `dupe_of` | the row this duplicates | exactly that | ✅ clear |
| `drain_notes` | get the notes and clear them | exactly that | ✅ the verb earns its place |
| `SourceHealth` | a source's current condition | that on read, an *event* on write | ⚠️ finding 6 |
| `FetchResult` / `FeedOutcome` | ? | two levels, two words, no rule | ⚠️ finding 7 |
| `upsert_items` | insert, updating on conflict | insert, **ignoring** on conflict | ❌ finding 2 |
| `new` | one thing | two different things in adjacent output | ❌ finding 1 |
| `LOOKBACK_HOURS` / `MAX_ENTRY_AGE_DAYS` | the same concept | request window vs post-filter | ⚠️ finding 9 |
| `kind` | the adapter discriminator | nothing; it is never read | ⚠️ covered in the layering review |

Most of the deliberate vocabulary work holds. `fetch_limit` vs `quota` — the pair most likely
to collide — is the clearest thing in the project, because it was named *against* a confusion
rather than into one. The failures are all in terms that were never explicitly chosen.

Ten findings.

---

## 1. "new" means two different things, both in user-visible output

**`src/digest/cli.py:151`** logs `new=%s` from `store.upsert_items`'s return — *rows inserted
this run*. **`src/digest/cli.py:118`** prints `f"{len(items)} new item(s)"` from
`store.new_items` (`store.py:400`) — *rows never rendered into a digest*.

**What is wrong.** One word, two definitions, in the same CLI, minutes apart.

**Why it matters.** The two can disagree completely and both be correct. Run `digest fetch`
twice: the second run logs `new=0` for every source. Run `digest render` immediately after and
it prints `425 new item(s)`. A stranger — or you in six months — reads those as contradictory
and starts debugging a dedupe bug that does not exist. It gets worse in 3c, when `render`
begins stamping `digest_date`: `new` will then shrink for one reason and not the other, and
the only way to know which "new" a number refers to is to know which code path printed it.

**Smallest fix.** Rename at the point of display, not in the store: log `inserted=` instead of
`new=` in `_log_source_line`, and print `unrendered item(s)` or `not yet in a digest` in
`_run_render`. `store.new_items` can keep its name — its docstring already defines it
precisely — but the two user-facing strings must stop sharing a word.

**Bucket: A.**

---

## 2. `upsert_items` does not upsert

**`src/digest/store.py:328`**, implemented with `INSERT OR IGNORE` at **`:346`**.

**What is wrong.** An upsert updates the existing row on conflict. This one discards the
incoming row entirely. The name promises the opposite of the behaviour.

**Why it matters.** The failure is timed for phase 4. `rank.py` and `summarise.py` will need to
write `score`, `score_reason`, `topic` and `summary` back onto stored items — columns that
already exist on the row and are already parameters of this function
(`store.py:363-365`). The obvious call is `upsert_items(conn, scored_items)`, it will return
`0`, and nothing will be written. No exception, no log line, and `new=0` looks exactly like a
normal second run. You would conclude the ranker produced no scores.

Worse, the function *accepts* those fields today, so it reads as though it handles them. It
only ever persists them for items that happen to be brand new.

**Smallest fix.** Rename to `insert_new_items` — accurate, and makes the missing capability
obvious at the call site rather than at debug time. The phase-4 write-back then arrives as a
deliberately separate `update_scores(conn, items)`, which is the honest shape anyway.

**Bucket: A.**

---

## 3. Errors about a bad datum never say which datum

**`src/digest/store.py:167`** (`"refusing to store naive datetime {value!r}; convert it
first"`), **`:181`**, **`:183`**, and **`src/digest/models.py:29`** (the `Item` validator).

**What is wrong.** Every one of these names the offending *value* and none names the offending
*item*: no URL, no source, no feed.

**Why it matters.** `Item` is constructed inside a list comprehension in every adapter — e.g.
`arxiv.py:104-109` maps 270 entries. One entry with an unparseable date raises a pydantic
`ValidationError`, which propagates out of the comprehension and fails the whole feed. The log
then reads roughly:

```
source arxiv_cs_ai failed: ValidationError: published_at must be timezone-aware; got naive ...
```

Which of 270 entries? Which category? You cannot tell. The recorded fixture lets you find it
eventually, but only by re-running locally — and if the bad entry came from a live feed rather
than the fixture, it is gone by the time you look. At 07:00 unattended, this is a source that
drops to zero with a message you cannot act on.

**Smallest fix.** Have adapters catch `ValidationError` around the single-item mapping and
re-raise with the URL attached — or, cheaper and with no per-adapter change, include
`item.url` in the store's messages, which is the layer that already has the whole `Item` in
hand. The adapters' `_item_from_entry` helpers returning `None` on missing title/link
(`arxiv.py:139`, `ai_blogs.py:234`) show the pattern already exists for *absent* data; it just
does not extend to *invalid* data.

**Bucket: A.**

---

## 4. Five exception types mean "this source is unusable", so a bug and an outage look identical

**`src/digest/adapters/hn.py:67`** `ValueError` · **`hf_papers.py:35`** `TypeError` ·
**`hf_papers.py:43`** `ValueError` · **`arxiv.py:76`** `RuntimeError` · **`ai_blogs.py:128`**
`RuntimeError` · **`gh_trending.py:53`** `GitHubTrendingError` · **`fetch.py:93`**
`LookupError`.

**What is wrong.** There is no `AdapterError` base. Every adapter picks a builtin more or less
by feel, and `fetch.py:101` catches bare `Exception`, flattening all of them into
`f"{type(exc).__name__}: {exc}"`.

**Why it matters.** The distinction that is lost is the one you most need at 07:00: *is this
our bug or their outage?* A deliberate `TypeError` at `hf_papers.py:35` ("the API returned a
dict, not a list") and an accidental `TypeError` from a coding mistake in the mapping produce
the same health record, the same footer line, and the same log shape. The first means
re-record a fixture and fix the mapping; the second means revert a commit. Nothing in the
output tells you which, and — because the source-failure path is *designed* to be survivable —
neither raises your suspicion.

This is the phase-1 lesson in a new form: correct broad catching plus an untyped raise means a
bug gets filed as a dead feed.

**Smallest fix.** One base class in `base.py`:

```python
class AdapterError(RuntimeError):
    """The source cannot be used: wrong shape, blocked, or unparseable. Not our bug."""
```

Have the seven deliberate raises use it (or subclasses), leave accidental exceptions as
builtins, and log the two at different levels in `_fetch_one`. `GitHubTrendingError` becomes
its subclass and the taxonomy is suddenly readable from a log.

**Bucket: A.**

---

## 5. `GitHubTrendingError` is the only named adapter exception, for an event two other adapters raise unnamed

**`src/digest/adapters/gh_trending.py:37`** — `class GitHubTrendingError(RuntimeError)`,
versus **`arxiv.py:76`** and **`ai_blogs.py:128`** raising bare `RuntimeError` for the same
category of event ("this whole source produced nothing usable").

**What is wrong.** The taxonomy is one adapter deep. Whichever adapter someone writes next,
they have two contradictory precedents in neighbouring files.

**Why it matters.** Concretely: `grep -r GitHubTrendingError` finds every place that failure is
raised, caught or tested. There is no equivalent query for "arXiv gave up" — you would grep for
a message substring, and the two messages have already drifted (`"every category failed
(3/3)"` vs `"all 6 feeds failed"`), so even that needs two patterns. The duplication review
found the same drift from the other direction; it is the same root cause.

**Smallest fix.** Subsumed by finding 4 — once `AdapterError` exists, both `RuntimeError`s
become it, and `GitHubTrendingError` becomes a subclass rather than an outlier.

**Bucket: A.**

---

## 6. `SourceHealth` names a state but carries an event on the write path

**`src/digest/models.py:71`**, written at **`fetch.py:113-117`**, read at
**`store.py:468`**.

**What is wrong.** "Health" is a condition. On the read path it is one: `consecutive_failures`
is a real running count. On the write path `fetch.py:117` sets it to `1` and its own comment
says "A flag, not a count". Same class, same field, two meanings, distinguished only by
direction of travel.

**Why it matters.** The layering review covered the mechanical consequence (the store must
infer success from `last_success_at is not None`). The *naming* consequence is that nothing
warns a caller. A future `run` command building a "complete" health record — carrying the
previous `last_success_at` forward, which is what the name invites — silently converts a
failure into a success. The name is what makes that mistake feel correct.

**Smallest fix.** Two names for the two roles: `SourceOutcome(name, succeeded_at)` for the
write, `SourceHealth` for the read. Touches `models.py`, which the phase scoping deferred, but
it is the rename that prevents the bug rather than documenting it.

**Bucket: A.**

---

## 7. Three levels of result, two words, and one level with no type at all

**`src/digest/fetch.py:31`** `FetchResult` (per run) · **`src/digest/adapters/ai_blogs.py:76`**
`FeedOutcome` (per feed) · **`arxiv.py:86`** `_fetch_feed` returning a bare `list[Item]` (per
feed).

**What is wrong.** "Result" and "Outcome" are synonyms used at different levels with no stated
rule, and the per-feed level has a named type in one bundle adapter and none in the other.

**Why it matters.** A stranger cannot answer "what do I return from a per-feed helper?" by
looking at the code, because the two existing bundle adapters answer differently. That is
exactly the question the sixth adapter asks, and whichever file they open first becomes the
precedent. It also blocks the `BundleAdapter` extraction the duplication review recommends:
you cannot write a shared hook signature until the two agree on what a per-feed result is.

**Smallest fix.** Pick one suffix and apply it at both levels — `FetchResult` / `FeedResult` —
and give arxiv the same type even though it only fills `items`. Uniformity at the seam is worth
one unused field.

**Bucket: A.**

---

## 8. The `BaseException` idiom has two instances, no shared base, and no discoverable home

**`scripts/snapshot_pipeline.py:53`** (`SnapshotError(BaseException)`) and
**`tests/conftest.py:112`** (`NetworkAccessInTestError(BaseException)`).

**What is wrong.** Both exist for the same reason, documented at length in both docstrings:
`fetch.py:101` catches every `Exception`, so a *harness* error raised inside an adapter gets
filed as a source failure. Both solve it by sitting outside `Exception`. Neither references a
shared base, and they live in different trees.

**Why it matters.** This has already happened twice — `SnapshotError` was written as a
`RuntimeError` first and had to be changed after a test failed with the wrong error message.
The third harness error will be written as a `RuntimeError` too, by someone who has read
neither docstring, and it will be swallowed silently rather than loudly. The idiom is correct;
it is simply not findable. There is no natural home for the shared base, since `scripts/` and
`tests/` do not import each other and neither should be imported by `src/` — that tension is
real and is why the duplication exists.

**Smallest fix.** Since a shared base has nowhere good to live, make the rule findable instead:
one line in `CLAUDE.md` beside the existing no-network-in-tests entry — *harness errors derive
from `BaseException` so they punch through `fetch.py`'s catch-all* — and a cross-reference in
each docstring. Documentation is the right fix here precisely because the code cannot be shared.

**Bucket: A.**

---

## 9. Three names for "how far back", two units, and six lines of prose to tell them apart

**`src/digest/adapters/hn.py:35`** `LOOKBACK_HOURS = 48` ·
**`src/digest/adapters/ai_blogs.py:55`** `MAX_ENTRY_AGE_DAYS = 30` · `interests.yaml:113`
`max_age_hours: 48`.

**What is wrong.** Three names for related-but-distinct windows. `hn.py:29-34` spends six lines
explaining that `LOOKBACK_HOURS` and `max_age_hours` are *not* the same concept and must not be
merged. That the code needs a paragraph to disambiguate two names is the finding.

**Why it matters.** None of the names says the thing that actually distinguishes them:
`LOOKBACK_HOURS` parameterises the outgoing *request* so old items are never fetched;
`MAX_ENTRY_AGE_DAYS` filters *after* parsing because RSS hands you the whole back catalogue;
`max_age_hours` governs *selection* into a digest. A reader who skips the comment will assume
they are the same knob in different units and "tidy" them into one — which silently undoes the
slow-burner fix that `LOOKBACK_HOURS` exists for, exactly as the comment fears.

**Smallest fix.** Name the stage, not the duration: `REQUEST_WINDOW_HOURS` (hn),
`INGEST_MAX_AGE_DAYS` (ai_blogs), leaving `max_age_hours` as the selection knob. The six-line
comment then becomes two, and the distinction survives someone who never reads it.

**Bucket: A.**

---

## 10. Two idioms for injecting a clock

**`src/digest/adapters/ai_blogs.py:60`** `_now()`, a module-level patch point, versus
`now=` parameters at **`store.py:331`** (`upsert_items`), **`store.py:301`**
(`is_near_duplicate`) and **`hn.py:54`** (`build_request`).

**What is wrong.** The same concern — make the clock injectable for tests — is solved two ways
in one codebase. Already filed in the `_now()` docstring as an R1 item; recording it here as
the vocabulary finding it is.

**Why it matters.** Small but compounding: the test-suite review found four `ai_blogs` tests
that expire on a calendar precisely because they do not use the patch point, while the
store's tests all pass `now=` and never expire. Two idioms means the safer one gets used only
where someone remembered it.

**Smallest fix.** Prefer the parameter — it is explicit at the call site and cannot be
forgotten by a test that does not know to patch. `Adapter.fetch(client, source)`'s fixed
signature is what blocked it, so the honest version is a `now` attribute set by `fetch.py`
before calling, or accepting the module function as the house idiom and saying so.

**Bucket: A.**

---

## Buckets

Ten findings, all **A**. **No B for the fifth dimension running**, which is now a pattern worth
addressing directly rather than re-testing a candidate each time.

The reason appears structural: CLAUDE.md's constraints are almost entirely *prohibitions on
adding things* — no ORM, no framework, no new dependencies, no network in tests, no LLM outside
`summarise.py`. Prohibitions of that shape can force you to hand-build machinery (which is
where the duplication review found real cost) but they cannot make you name things badly or
raise the wrong exception type. Naming and taxonomy are unconstrained by every rule in the
file. So a clean sweep of **A** here is what you would predict, and is weak evidence about the
constraints rather than strong evidence for them.

The one place a constraint genuinely shapes this dimension is finding 8, and it shapes it
*correctly*: the reason the `BaseException` idiom cannot be shared is that `src/` must not
import from `tests/` or `scripts/`, which is right.

### One C

**`src/digest/fetch.py:101`** — `except Exception` around every adapter call.

Finding 4 invites the fix "catch `AdapterError` instead of `Exception`". That would be a
regression. An accidental `TypeError` or `AttributeError` inside an adapter would then escape
`_fetch_one`, reach `fetch_all`'s belt-and-braces branch at `:158-163`, and — while still not
aborting the run — bypass the health record and the notes drain, so the failure would be logged
without the per-feed diagnostics that explain it. Narrowing the catch trades a diagnosable
failure for a quieter one.

The value of `AdapterError` is in *classifying* what is caught, not in narrowing the catch.
Catch everything, then say which kind it was.

### If you take only two things

**Finding 2**, because the payload is timed for phase 4 and the symptom is a silent no-op —
`upsert_items(conn, scored_items)` returning `0` and writing nothing looks identical to a
normal second run. **Finding 1**, because it is two strings and it removes a contradiction the
tool currently prints about itself.
