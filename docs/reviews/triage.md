# R2 — Triage of the interphase review

54 findings across six dimensions. Every one has a decision below. Themes are ordered; R3 runs them
in that order and one per session.

Shorthand for sources: **SF**=Silent failure, **LC**=Layering, **DUP**=Duplication, **TS**=Test suite,
**VN**=Vocabulary, **DD**=Documentation drift.

---

## The one B — decide before any code is written

**DD-1: the Definition of Done is satisfied by a no-op.** `uv run digest run --dry-run` prints
"not implemented" and exits 0, so the first clause of the project's quality gate cannot fail. Accepted
as a B: the intent is right, the instrument is broken, and the risk is live.

**Decision — amend `CLAUDE.md`'s Definition of Done to:**

```
uv run pytest                      # full suite, offline
uv run pytest tests/test_snapshot.py   # composition unchanged, or change approved in writing
uv run ruff check && uv run ruff format --check
```

Drop the `digest run --dry-run` clause entirely until `run` exists; re-add it in 3c with a real
assertion about the file it writes. R0 already delivered the better instrument and `CLAUDE.md` never
learned about it. **This edit lands before R3 session 1.**

---

## Theme 1 — Test-suite determinism and honesty  *(run FIRST)*

First because several of these tests will fire spuriously during the later themes' renames, and
because one of them breaks on its own in seven days.

| Finding | Decision | Note |
|---|---|---|
| TS-1 four `ai_blogs` tests expire on future dates | **fix now** | Pin to `manifest.json` exactly as R0 did. The suite going red on a calendar is bad; training you to re-record fixtures — the same response a real upstream death demands — is worse |
| TS-8 assertions pinned to one recording day's stats | **fix now** | Same fix |
| TS-5 `test_the_clock_is_pinned_to_the_fixture_set` passes if pinning is deleted | **fix now** | Make it load-bearing; it currently certifies the design R0 rejected |
| TS-4 `test_every_selector_is_a_module_constant` breaks on a harmless rename | **fix now** | Must precede Theme 4/5 renames or it fires as noise |
| TS-3 nothing asserts the User-Agent is sent | **fix now** | A hard constraint, invisible in 178 tests, visible only as a 403 at 07:00 |
| TS-7 snapshot suite is 6.8s of a 12s run | **fix now** | Module-scoped fixture. R3 runs this test constantly |
| VN-10 two idioms for injecting a clock | **fix now** | Decide here: parameter injection (`now=`) is the house idiom, since `store` already does it. Convert `ai_blogs._now()` |
| TS-9 `test_raw_contains_no_clock` asserts an exact key set | **won't fix** | The strictness is the point — it is the `scraped_at` regression test |
| TS-6 no test exercises a timeout expiring | **fix later** | Trigger: next change to timeout handling, or the third bundle adapter |
| TS-2 `digest render` has no tests | **fix later** | Trigger: 3c, which rewrites render anyway |

---

## Theme 2 — `SourceHealth`: split the event from the state

| Finding | Decision | Note |
|---|---|---|
| LC-1 / VN-6 `SourceHealth` is two models sharing a name; the store guesses success | **fix now** | `record_source_health(conn, name, *, succeeded_at: datetime \| None)`. One call site. The named break — a retry wrapper carrying `last_success_at` forward and silently resetting the counter — is exactly the two-correct-things collision this project keeps producing |
| SF-3 failure history destroyed on recovery | **fix now** | Minimal form only: add `last_failure_at` and `total_failures` to the `sources` row. Do **not** build a `runs` table here; Phase 4 brings one |

Before 3c, because 3c's footer reads this.

---

## Theme 3 — Unattended-run signals

Everything here is about a run nobody is watching. Phase 5 is where these stop being cosmetic.

| Finding | Decision | Note |
|---|---|---|
| SF-1 a run where every source fails exits 0 | **fix now** | Highest-consequence finding in the review: it would satisfy PLAN §0's seven-consecutive-days criterion while delivering nothing |
| SF-2 the mandated run log is at INFO, below default WARNING | **fix now** | Nobody passes `-v` in a crontab |
| SF-6 `digest render` silently creates an empty database | **fix now** | A typo'd `--db` currently yields a confident empty digest |
| SF-7 the footer cannot distinguish "disabled" from "ran and returned nothing" | **fix now** | Before 3c builds the real footer. **Amended 2026-09-14 by Theme 2:** this row now also owns *surfacing* `total_failures` and `last_failure_at`, which Theme 2 added to the `sources` row but deliberately did not display. SF-7 as originally written is about disabled-versus-empty, which is adjacent but not the same obligation — and two columns nobody reads are exactly what a later review finds and proposes deleting. Without the footer line, SF-3's stated consequence (a feed failing every other day reads as healthy every time you look) is recorded but still not *visible* |
| SF-8 arXiv's announce filter can drop 100% of input and report success | **fix now** | Same shape as the staleness/cutoff interaction — a filter blinding the measurement behind it |
| SF-9 `durations` read with `getattr`, so a rename degrades to 0.00s | **fix now** | One line; textbook silent degradation |
| SF-4 a run that inserts nothing leaves no trace it happened | **fix later** | Trigger: Phase 4's `runs` table. "Did Tuesday's job run?" must be answerable before Phase 5 ships |
| SF-5 per-feed notes printed once, never stored | **fix later** | Same trigger |

**C guard, carried into the prompt:** the obvious reading of SF-1 is "stop swallowing exceptions in
`fetch.py`". That would reintroduce the failure the never-abort rule prevents. The fix is the exit
code, not the catch.

---

## Theme 4 — Exception taxonomy

| Finding | Decision | Note |
|---|---|---|
| VN-8 the `BaseException` idiom has two instances, no shared base | **fix now** | `DigestControlError(BaseException)`, docstring carrying the test: *the program's assumptions about its own execution are violated, not a data source misbehaved*. `NetworkAccessInTestError` and `SnapshotError` become subclasses. Load-bearing test: a subclass raised inside an adapter escapes `fetch_all` intact. Three discoveries by failing-test-message is two too many |
| VN-4 five exception types mean "source unusable"; a bug and an outage look identical | **fix now** | Introduce `AdapterError` as a *classification*, and keep `except Exception`. The phase-1 lesson in new form: a bug filed as a dead feed |
| VN-5 `GitHubTrendingError` is the only named adapter exception | **fix now** | Fold into the same hierarchy |
| VN-3 errors about a bad datum never name the datum | **fix now** | One unparseable date currently fails a 270-entry feed with a message naming neither the entry nor the URL |
| VN-7 three levels of result, two words, one level untyped | **fix later** | Trigger: a fourth result type appears |

**C guard:** do not narrow `fetch.py`'s catch to `AdapterError`. An accidental `TypeError` would then
bypass the health record and the notes drain. The value of a named base is classifying what is caught,
never narrowing the catch.

---

## Theme 5 — Registry, instances and dead config

| Finding | Decision | Note |
|---|---|---|
| LC-2 the registry conflates "which implementation" with "which source" | **fix now**, minimal scope | Key the registry by source name and construct one adapter instance per source. This closes LC-4 as a side effect |
| LC-4 `drain_notes` is stateful on a process-lifetime singleton | **fix now** | Closed by LC-2. Today the natural reaction to LC-2 — registering one instance under two names — silently misattributes feed diagnostics with no exception and no failing test |
| LC-9 `Source.kind` is dead config that looks load-bearing | **fix now** | Make it the dispatch key, or delete it. Do not leave it declared, printed, and dispatched on nowhere |
| DUP-1 bundle fan-out exists twice and has drifted | **fix later** | Trigger: the third RSS bundle adapter (`gh_releases`). Extracting over two implementations means guessing the hook signature while the two disagree on what a per-feed result is. **But** fix the all-feeds-empty-but-successful gap in *both* copies now, under Theme 3 |
| DUP-5 `__init__`/`drain_notes` duplicated in both bundle adapters | **fix later** | Same trigger |
| LC-3 the pipeline's composition lives in an argparse handler | **fix later** | Trigger: 3c, which composes the third stage. Cheaper to build the orchestrator when there are three stages than to build it now and rewire it then |

---

## Theme 6 — Shared helpers and reconciled vocabulary

| Finding | Decision | Note |
|---|---|---|
| DUP-2 `_published_at` byte-identical in two files | **fix now** | It enforces the project's oldest invariant (`Item` rejects naive datetimes). A correction currently has to be made twice |
| DUP-6 ISO-with-`Z` parsing duplicated between the JSON adapters | **fix now** | Same helper |
| DUP-3 HN titles are the only ones not whitespace-normalised | **fix now** | Feeds the Dice tokeniser, which 3b depends on |
| DUP-4 / LC-8 the `fetch_limit` slice is copied four times and means something else in the fifth | **fix now** | Reconcile the semantics, not just the code |
| VN-9 three names for "how far back", two units | **fix now** | Naming only. Keep the concepts distinct — ingest window and `max_age_hours` must stay separable — but name them so the distinction is visible |
| LC-5 HN's points floor is stated in two config files with nothing reconciling them | **fix now** | Assert equality at load, or derive one from the other |
| VN-1 "new" means two different things in adjacent user-visible output | **fix now** | `new=0` per source followed by `425 new item(s)` reads as a contradiction; both are correct |
| VN-2 `upsert_items` does not upsert | **fix now**, rename only | Rename to `insert_new_items`. 3b will want to write `score`/`topic` back onto stored rows and the obvious call would return 0 and write nothing, indistinguishable from a normal second run. The real write-back path is 3b's job, not this theme's |
| LC-6 ingest-window policy hardcoded, unaware of the config governing it | **fix later** | Trigger: 3b already owes the `LOOKBACK_HOURS >= max_age_hours` assertion |
| LC-7 `scripts/` depends on a dev-only test library, `tests/` depends on `scripts/` | **fix now** | A packaging landmine for Phase 5 CI |

---

## Theme 7 — Documentation  *(run LAST)*

Last, because it documents what the other six themes changed.

| Finding | Decision | Note |
|---|---|---|
| DD-1 Definition of Done | **decided above** — lands before session 1, not here |
| DD-2 README describes a phase-0 skeleton | **fix now** | First file a stranger opens. Add `--db`; mark `rank`/`run` as stubs |
| DD-4 `base.py` names the wrong module as run-log owner | **fix now** | First file the sixth adapter's author reads |
| DD-3 "Back off on 429" is a hard constraint with no implementation | **fix now (wording)** / **fix later (behaviour)** | Hard constraints read as descriptions of current behaviour. Move it to a "not yet implemented" section. Trigger for the behaviour: the first 429, or adding Reddit — which throttles hard and is next in Tier 2 |
| DD-5,6,7,8,9,10,11 PLAN drift | **fix now** | One documentation commit, using the append-a-dated-amendment shape §4 already uses |

**C guard, and it matters here:** do **not** rewrite PLAN §7's per-phase "Done when" lines to match
what shipped. §7 is a record of intent at a point in time, and in a project whose recurring bug class
is two correct decisions interacting, its value is preserving what was believed before the code taught
otherwise. Append a dated amendment; never overwrite.

**Applied 2026-09-15.** All rows above are done. The guard generalised into a rule that is now
stated in PLAN §7.1, because it has to be usable by someone who was not in the conversation:
*a record of intent gets an amendment; a catalogue of measurements gets a correction.* §7 and
§3's intentions were amended and dated; §2's counts were corrected **and dated on both sides**,
since a number without a date reads as a constant and that is precisely how the arXiv row
misled — 50 items measured once became a threshold, and the real figure moves between 16 and
270 depending on the category and the day. `base.py` and README are neither kind of record:
they describe what is true now, so they were rewritten.

Theme 7 also swept for drift the earlier six themes created. Found and fixed: `base.py`
stating that `kind` is "free-form text" when Theme 5 made it the required dispatch key;
`hn.py` calling the ingest-window assertion "the right phase 3 move" after phase 3a had
shipped without it; `sources.yaml` still naming `LOOKBACK_HOURS`; `errors.py` and a test
docstring still saying three adapters have no notes *channel*; a test named
`test_304_is_no_unrendered_items_not_an_error`, where Theme 6's `new` → `unrendered` rename
landed on a 304 — which means no new *entries* and has nothing to do with rendering; and
`MAX_ENTRY_AGE_DAYS` surviving in the fixture manifest and in `found-during-r3.md` itself.

---

## Deferred, with triggers

**Third column added by Theme 7 (2026-09-15).** A trigger recorded only here fires only if
someone rereads this file, which is the one thing you cannot schedule. Each row below is now
also written where the work would actually be done — a docstring, a constant, a config
comment — so the trigger reaches whoever is editing that code whether or not they came from
here. This table stays the index; the planted copies are the mechanism.

| Item | Trigger | Planted in |
|---|---|---|
| Bundle extraction (DUP-1, DUP-5) | The third RSS bundle adapter | `adapters/_mapping.py` module docstring |
| Orchestrator module (LC-3) | 3c, composing the third stage | `cli.py::_run_fetch` docstring |
| Run provenance / `runs` table (SF-4, SF-5) | Phase 4 | `models.py::SourceOutcome.expected_failure` |
| Timeout-expiry test (TS-6) | Next change to timeout handling | `fetch.py::SOURCE_TIMEOUT_SECONDS` |
| `render` tests driving `main()` (TS-2) | 3c | `cli.py::_run_render` docstring |
| Result-type naming (VN-7) | A fourth result type | `models.py::SourceOutcome.expected_failure` |
| 429 backoff behaviour (DD-3) | First 429, or adding Reddit | CLAUDE.md, "Not yet implemented" |
| Conditional-GET validators (DD-7) | A host rate-limiting us | PLAN §3; `ai_blogs.conditional_headers`; `test_conditional_headers_are_empty_until_validators_are_persisted` |
| Ingest-window assertion (LC-6) | 3b (already owed) | `adapters/hn.py::REQUEST_WINDOW_HOURS` |
| Per-source fixture `captured_at` (R3-1) | Any edit to `record_fixtures.py` | `scripts/record_fixtures.py::write_manifest` |
| Skip-and-count unmappable entries (R3-3) | DUP-5, or the first real mapping failure | `errors.py::unmappable_entry` |

DD-7 joined this table in Theme 7: PLAN §3 mandated conditional GET on every RSS source and
nothing recorded that it had been half-built and stopped, which made it a silent obligation
rather than a deferred one.

## Won't fix

| Item | Reason |
|---|---|
| TS-9 exact key set in `test_raw_contains_no_clock` | The strictness is the point; it is the `scraped_at` regression test |

## On the absence of B

Five dimensions produced no B, and the sixth produced exactly one. The reviewer's structural
explanation is accepted: `CLAUDE.md`'s constraints are almost entirely prohibitions on *adding*
things, and a prohibition can force you to hand-build machinery but cannot make you name things badly
or pick the wrong exception type. The single B arrived in the one dimension where the documents
themselves are the constraint. That is a coherent result, not a reviewer going easy — and the two
strongest near-misses were each tested and rejected in writing rather than promoted to fill the bucket.
