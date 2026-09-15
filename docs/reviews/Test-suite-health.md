# Review: test suite health

Interphase review, dimension 4. Reviewed against `CLAUDE.md` and `docs/PLAN.md` at commit
`23d0b45` — 178 tests across 9 modules, 3.0 MB of fixtures in 13 files. **No files were
changed.**

Distribution: `test_store.py` 35, `test_hn.py` 24, `test_ai_blogs.py` 22, `test_arxiv.py` 17,
`test_skeleton.py` 16, `test_gh_trending.py` 14, `test_hf_papers.py` 11, `test_snapshot.py` 6,
`test_cross_source_dupes.py` 5.

**The suite is in good shape on the axis it was built for and has one serious structural
problem: parts of it expire on a calendar.** Nine findings. The first is dated and will fire
within a week.

---

## 1. Four `ai_blogs` tests fail on specific future dates with no code change

**`tests/test_ai_blogs.py:205`** (`test_recorded_feeds_are_all_currently_fresh`),
**`:66`** (`test_all_six_feeds_contribute`), **`:80`** (`test_dates_come_back_timezone_aware`),
**`:88`** (`test_titles_are_whitespace_normalised`).

**What is wrong.** `AIBlogsAdapter` measures staleness and the 30-day ingest cutoff against
`ai_blogs._now()`, and these tests let it use the real clock while feeding it fixtures frozen
at 2026-09-14. R0 solved exactly this for the snapshot by pinning the clock to
`manifest.json`; the adapter tests never got the same treatment.

**Why it matters.** Measured against the committed fixtures, with each feed's configured
threshold:

| feed | newest entry | threshold | goes STALE | all entries filtered |
|---|---|---|---|---|
| openai_news | 2026-09-14 | 7d | **2026-09-21** | 2026-10-14 |
| claude | 2026-09-14 | 14d | 2026-09-28 | 2026-10-14 |
| deepmind | 2026-09-08 | 21d | 2026-09-29 | 2026-10-08 |
| anthropic_news | 2026-09-01 | 30d | 2026-10-01 | **2026-10-01** |

`test_recorded_feeds_are_all_currently_fresh` asserts no note contains `STALE`, so it fails on
**2026-09-21** — seven days from now. `test_all_six_feeds_contribute` asserts all six feeds
yield items, so it fails on **2026-10-01** when `anthropic_news`'s newest entry passes the
30-day cutoff. The other two then follow as their item lists empty out.

The consequence is worse than a red suite. A test that fails for calendar reasons trains you to
respond by re-recording fixtures, which is also the correct response to a *real* upstream
change — so the two become indistinguishable, and the day a feed genuinely dies you will
refresh past it without noticing. It also means the suite cannot be trusted on an old checkout:
`git bisect` across anything older than a week will report failures that have nothing to do
with the commits being bisected.

**Smallest fix.** Give `tests/conftest.py` a fixture that reads `captured_at` from
`manifest.json` and patches `ai_blogs._now`, and use it in `test_ai_blogs.py`. The manifest and
the seam both already exist — this is wiring R0's solution into the tests that need it most.
`test_recorded_feeds_are_all_currently_fresh` should keep the real clock deliberately, but as
an explicitly-marked freshness check of the *configured feeds*, not a unit test.

**Bucket: A.**

---

## 2. `digest render` has no tests at all

**`src/digest/cli.py:96-129`** — grepping the suite for render coverage returns one hit, and
it is `test_store.py:373::test_new_items_are_those_never_rendered`, which tests
`store.new_items`, not the command.

**What is wrong.** One of the two live subcommands is entirely untested: the grouping by
source, the `published_at`-or-`?` formatting, the empty-database path, and the exit code.

**Why it matters.** This is the command whose output you read every morning, and in 3c it
becomes the Markdown renderer. Three concrete failures it would not catch today: an item with
`published_at=None` (every `gh_trending` row) hitting the `strftime` branch; the
silent-empty-database path at `:110-112` that the observability review flagged, where a typo'd
`--db` prints "Nothing new" indistinguishably from success; and a multi-line title breaking the
two-line layout at `:123-124`. Coverage is thinnest exactly where the user-visible surface is.

**Smallest fix.** Three tests against a `tmp_path` database seeded through `store.upsert_items`:
items present and grouped, empty database, and one item with `published_at=None`. No fixtures
and no network needed.

**Bucket: A.**

---

## 3. Nothing asserts the User-Agent is actually sent

**`src/digest/fetch.py:138`** sets `headers={"User-Agent": user_agent()}`. The only matches for
"User-Agent" in `tests/` are inside the fixture-recording recipe in a docstring
(`test_hn.py:20,30`) — not assertions.

**What is wrong.** A CLAUDE.md hard constraint — "Set a real User-Agent on every outbound
request" — has zero test coverage, and neither does `fetch.user_agent()`'s
`DIGEST_USER_AGENT` override.

**Why it matters.** The failure is remote, silent and delayed. Refactor `fetch_all`'s client
construction and drop `headers=`, and every test still passes, because `MockTransport` does
not care what headers arrive. The first symptom is `gh_trending` returning 403 at 07:00 — the
exact failure its own error message at `gh_trending.py:53-57` tells you to check the
User-Agent for. Reddit, which PLAN §2 Tier 2 adds next, throttles default agents the same way.

**Smallest fix.** One assertion in the existing `run_fetch_all` helper: capture the request in
the mock handler and assert `request.headers["user-agent"]` is non-default. The capture
mechanism already exists in `test_hn.py:71-77`.

**Bucket: A.**

---

## 4. `test_every_selector_is_a_module_constant` breaks on a harmless rename

**`tests/test_gh_trending.py:141-151`** — `inspect.getsource(gh_trending.parse_trending)` and
`inspect.getsource(gh_trending._item_from_row)`, then substring checks for `"Box-row"` and
`"itemprop"`.

**What is wrong.** It tests source text, and names two private functions explicitly. Renaming
`_item_from_row`, splitting it, or inlining it into `parse_trending` — all behaviour-preserving
— fails the test with `AttributeError` rather than a useful message.

**Why it matters.** The intent is good (a layout change should be a one-line fix), but the
mechanism penalises exactly the refactoring the interphase review exists to enable. A moved
selector into a *third* helper would also pass while violating the rule, so it is both brittle
and incomplete.

**Smallest fix.** Scan the whole module rather than two named functions, and exclude the
constants block: read `inspect.getsource(gh_trending)`, drop the lines between the two
`# --- Selectors` markers, and assert the literals appear nowhere in the remainder. Survives
renames, and catches a selector inlined anywhere.

**Bucket: A.**

---

## 5. `test_the_clock_is_pinned_to_the_fixture_set` would pass if the pinning were deleted

**`tests/test_snapshot.py:93-102`** — asserts `manifest.json` exists and
`snapshot_now().tzinfo is not None`.

**What is wrong.** Neither assertion checks the behaviour the name claims. Replace
`snapshot_now()`'s body with `return datetime(2026, 9, 14, tzinfo=UTC)` — the hardcoded
constant the design explicitly rejected — and this test still passes.

**Why it matters.** The manifest indirection exists so that re-recording moves the pinned clock
with the fixtures; without it the snapshot silently becomes a snapshot of nothing. That is the
single most important property of the R0 work, and its test does not test it. Someone
simplifying `snapshot_now` into a constant would get a green suite.

**Smallest fix.** Write a temporary manifest with a different `captured_at` into a `tmp_path`,
point `MANIFEST_PATH` at it via monkeypatch, and assert `snapshot_now()` returns that value.
Three lines, and it pins the actual behaviour.

**Bucket: A.**

---

## 6. No test exercises a timeout actually expiring

**`src/digest/fetch.py:97`** (`asyncio.timeout(SOURCE_TIMEOUT_SECONDS)`),
**`ai_blogs.py:140`** and **`arxiv.py:89`** (per-feed `asyncio.timeout`).

**What is wrong.** The per-feed isolation tests simulate slowness by raising
`httpx.ConnectTimeout` (`test_ai_blogs.py:238`), which tests the *exception path*, not the
timeout mechanism. Nothing verifies that a feed which merely hangs is actually cut off.

**Why it matters.** The whole point of the per-feed budget — the phase-1 note the 3a work was
built to discharge — is that one slow feed must not consume the shared 20s and cost the other
five. A hanging socket and a `ConnectTimeout` are different code paths: the first depends on
`asyncio.timeout` firing and on the timeout being *inside* the gathered coroutine. Move the
`async with` one level out during a refactor and every existing test still passes, while a
single hung feed starts costing the whole source. Unattended at 07:00 that is a source that
silently contributes nothing.

**Smallest fix.** One test with an async mock handler that `await asyncio.sleep(0.2)`s, and
`FEED_TIMEOUT_SECONDS` monkeypatched to `0.01`. Asserts the other feeds still return and the
slow one is noted as failed. Fast, no real waiting.

**Bucket: A.**

---

## 7. The snapshot suite runs the full pipeline six times, costing half the suite's runtime

**`tests/test_snapshot.py`** — `generate()` is called six times across five tests, and
`test_generation_is_deterministic:90` calls it twice by itself.

**What is wrong.** Measured: `test_generation_is_deterministic` 2.65s,
`test_every_source_contributes_rows` 1.30s, `test_pipeline_matches_the_committed_snapshot`
1.29s, `test_the_snapshot_exercises_the_age_cutoff` 1.28s. That is ~6.8s of a ~12s suite for
six invocations of the same deterministic function over the same 3 MB of fixtures.

**Why it matters.** Not correctness, but the suite is the thing you run between every edit, and
more than half its wall-clock is one function re-parsing 572 KB of arXiv XML and 717 KB of
OpenAI RSS repeatedly. It will only grow as sources are added.

**Smallest fix.** A module-scoped fixture holding one `generate()` result for the four tests
that only *read* the dump. `test_generation_is_deterministic` legitimately needs two calls —
that is its subject — and `test_a_missing_fixture_fails_loudly` needs its own. Cuts roughly
4s without losing an assertion.

**Bucket: A.**

---

## 8. Two assertions are pinned to one recording day's statistics

**`tests/test_arxiv.py:64-68`** (`test_the_announce_filter_is_load_bearing`, asserting the
revision share exceeds 0.25) and **`tests/test_cross_source_dupes.py:64`**
(`test_the_same_paper_really_does_arrive_from_both_sources`).

**What is wrong.** Both encode a property of the *day the fixtures were recorded*, not of the
code. 2026-09-14 happened to have a 38% revision share and exactly one cross-source duplicate.

**Why it matters.** A re-record on a quiet Sunday could yield a 20% revision share, or zero
overlapping papers, and both tests fail with nothing wrong. The cross-source test already
anticipates this — its failure message tells you to re-record both sources on the same day —
which is the right instinct but means a legitimate refresh can leave you unable to make the
suite green without retrying on a different day.

**Smallest fix.** For the arXiv one, assert `> 0` revisions exist rather than a ratio — the
point is that the filter has something to do, and the 38% figure belongs in the docstring as a
recorded observation, which it already is. For the cross-source one, keep the assertion but
consider pinning a second, tiny fixture pair containing the known-duplicate paper, so the
mechanism stays tested even on a refresh that loses the overlap.

**Bucket: A.**

---

## 9. `test_raw_contains_no_clock` asserts an exact key set

**`tests/test_gh_trending.py:172`** — `assert set(items[0].raw) == {"full_name",
"description", "language", "stars_today"}`.

**What is wrong.** Adding a legitimate field — `stars_total`, `topics`, `owner_avatar` — fails
a test whose stated subject is "no clock in raw".

**Why it matters.** Minor, but it is the shape that makes a suite feel obstructive: a test that
fails for a reason unrelated to its name. The very next line already proves the real property
by hashing two runs and comparing.

**Smallest fix.** Replace the exact-set assertion with `assert not any("at" in k and "time" in
str(type(v)) ...)` — or more simply, keep the two-run hash comparison and assert
`"scraped_at" not in items[0].raw`, naming the thing being guarded against.

**Bucket: A.**

---

## Is the fixture set carrying its weight?

**Mostly yes, with one caveat about growth.** 3.0 MB across 13 files, dominated by
`gh_trending.html` (748 KB), `ai_blogs_openai_news.xml` (717 KB), `arxiv_cs_ai.xml` (572 KB)
and `hf_papers.json` (353 KB).

**Earning it:** `gh_trending.html` is the only thing standing between you and silent selector
rot on the project's most fragile adapter, and trimming it to the `<main>` region — tempting,
since ~90% is navigation chrome and inline SVG — would stop catching the case where `Box-row`
moves into a different container. Keep it whole. `ai_blogs_openai_news.xml` with all 1193
entries is what makes the back-catalogue finding testable at all; a trimmed copy would not have
surfaced the 1824-item bug in the first place. `arxiv_cs_ai.xml` at 270 entries is what makes
the 38%-revision measurement real rather than asserted.

**The caveat is git, not disk.** Every `--source all` refresh writes 13 new blobs, ~3 MB, and
git keeps all of them. Monthly refreshes for a year is ~36 MB of history for a repo whose code
is under 200 KB. That is survivable, but it argues for refreshing *per source* rather than
wholesale — `record_fixtures.py --source arxiv` already supports this and is the cheaper habit.

**The refresh story is good and has one gap.** `record_fixtures.py` writes `manifest.json` on
every run so the pinned clock moves with the fixtures, each test module names the command that
produced its fixtures, and the adapter tests are designed to fail loudly when a source changes
shape — `test_null_url_falls_back_to_the_hn_thread:194` and
`test_recorded_feeds_are_all_currently_fresh` both carry messages telling you what to re-record.
The gap is that a refresh currently requires a *second*, separate step —
`uv run python scripts/snapshot_pipeline.py` — which nothing enforces or reminds you about. A
refresh without it leaves `test_pipeline_matches_the_committed_snapshot` red for a reason that
is legitimate but looks alarming.

---

## Which tests would pass if the code they cover were deleted?

Three, all named above: **finding 5** (the clock-pinning test passes against a hardcoded
constant), **finding 6** (the timeout tests pass without any timeout, because they only
exercise the exception path), and a partial case — `test_no_placeholder_survives_substitution`
(`test_hn.py:118`) asserts no `{` survives, which remains true if the unresolved-placeholder
*guard* at `hn.py:80-86` is deleted, since `str.replace` already removed it. That one is
covered by its sibling `test_typoed_placeholder_fails_loudly:123`, so the guard is not
unprotected — but the test itself proves less than its placement suggests.

Everything else I checked does fail when its subject is removed. The load-bearing tests
introduced in phases 2 and 3a — `test_the_numeric_guard_is_load_bearing`,
`test_the_announce_filter_is_load_bearing`, `test_a_layout_change_raises_rather_than_returning_empty`
— are genuinely load-bearing, which is the convention in CLAUDE.md doing its job.

---

## Buckets

Nine findings, all **A**. **No B for the fourth dimension running**, so here is the candidate
tested rather than asserted.

**Candidate: "No network access in tests", enforced by the autouse guard in
`tests/conftest.py`.** It looks implicated in finding 1: the tests expire *because* they cannot
refresh themselves. But the causation does not hold. The tests expire because they read the
real clock while their data is frozen, and the fix — pinning the clock from the manifest — is
entirely offline and already implemented once, for the snapshot. The network guard is what
makes the suite honest about that fixed data rather than papering over it; without it, finding
1 would present as a flaky test that passes on machines with fresh network access. The
constraint is diagnosing the problem, not causing it.

**No C findings.** Nothing here is a case where the obvious improvement would reintroduce a
known failure.

### If you take only two things

**Finding 1**, because it fires on 2026-09-21 and because its failure mode trains a bad habit —
reaching for a fixture refresh, which is also the correct response to a real upstream change,
so the two stop being distinguishable. **Finding 3**, because it is one assertion against a
hard CLAUDE.md constraint whose violation is invisible in every test and visible only as a 403
at 07:00.
