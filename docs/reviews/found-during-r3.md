# Found during R3

Things noticed while applying a theme, outside that theme's scope. Not fixed. Recorded so
they are not rediscovered.

**Triaged in Theme 7 (2026-09-15).** All three are resolved as deferred-with-a-trigger; none
needed a fresh decision, because each already had one implied by an earlier theme. What Theme
7 added is the part that makes a trigger work: each is now written into the file a future
session must open in order to hit it, not only into this one. The `triage.md` deferred table
carries the same list with its planted location, and PLAN §7.1 indexes both. Names in the
entries below were corrected to post-Theme-6 spellings; the findings are unchanged.

---

## R3-1 — a partial fixture refresh stamps a whole-set `captured_at`

**Found during:** Theme 1, wiring `fixture_captured_at()` into the `ai_blogs` tests.
**Files:** `scripts/record_fixtures.py` (`write_manifest`, and its call at the end of `main`).

`main` calls `write_manifest(names, started_at)` where `names` is only the sources recorded on
*this* run and `started_at` is the moment the run began. So
`record_fixtures.py --source arxiv` — the per-source refresh the test-suite review
specifically recommended as the cheaper habit, to keep git history small — rewrites
`captured_at` for the entire fixture set while leaving twelve of the thirteen files untouched.

**Concrete consequence, now that Theme 1 has landed.** Both `tests/conftest.py`'s
`fixture_captured_at()` and `scripts/snapshot_pipeline.py`'s `snapshot_now()` read that single
`captured_at` and pin the `ai_blogs` clock to it. Refresh only arXiv a month from now and the
pinned clock jumps forward a month while the `ai_blogs` fixtures stay where they were — which
pushes their entries past `INGEST_MAX_AGE_DAYS`, empties the source, and fails the `ai_blogs`
tests and the snapshot for a reason that has nothing to do with arXiv.

That is the same shape as the bug Theme 1 just fixed, arriving through the refresh path
instead of the calendar. It is latent today only because the whole set was captured in one
run.

**Shape of a fix (not applied).** Record `captured_at` per source rather than one for the set —
`{"sources": {"arxiv": {"captured_at": ...}, ...}}` — and have `fixture_captured_at(source)`
take the source it is reading. `snapshot_now()` would then need a rule for the whole-pipeline
case; the oldest `captured_at` is the conservative choice, since it is the only instant at
which every fixture is simultaneously valid.

**Why not now.** It changes the manifest format, which `snapshot_now()` and the snapshot test
both read, and Theme 1's brief was explicit that the snapshot must not move. It also wants
deciding alongside whatever Theme 7 says about the refresh story.

**Suggested trigger:** the first per-source refresh, or any change to `record_fixtures.py`.
Until then, refresh with `--source all` so the manifest and the fixtures stay honest with each
other.

**Triaged 2026-09-15: deferred, trigger planted.** Theme 7 wrote the whole of the above —
the hazard, the `--source all` instruction, and the shape of the fix — into
`write_manifest`'s docstring in `scripts/record_fixtures.py`, which is the function anyone
touching the recorder reads. "Any change to this file" is the trigger and the file now says
so. Not fixed here because Theme 7 changes no behaviour and the fix moves the manifest format
that `snapshot_now()` reads.

---

## R3-2 — a Theme 3 edit silently did not apply, and the test did not catch it

**Found during:** Theme 4, reading `cli.py` for the exit-code wiring.
**Files:** `src/digest/cli.py` (`_run_render`). **Fixed in the Theme 4 commit**, because it
was a defect in work already reported as complete rather than a new finding.

Theme 3's SF-7 change had two call sites: `_run_fetch` and `_run_render`. The scripted edit
for the second did not match (an escaping slip in the patch string), so `_run_render` kept
calling `_health_summary(items, health)` without `enabled=` and returning a bare `0`. A
disabled source therefore rendered as `quiet` in `digest render` while rendering correctly as
`disabled` in `digest fetch`.

**Why no test caught it.** `test_the_footer_distinguishes_every_source_state` calls
`_health_summary` directly with `enabled=` supplied. It tested the *function* and not the
*call site*, so it passed against a caller that never passed the argument.

That is a general gap worth naming, not a one-off: several tests in this suite exercise a
helper rather than the path that uses it. The snapshot covers composition for `fetch`, but
`render` has no test at all -- TS-2, deferred against 3c -- which is exactly why this could
sit unnoticed.

**Suggested trigger:** 3c, which rewrites `render` and owes it tests (TS-2). When those land,
at least one should drive `main(["render", ...])` end to end rather than calling the
formatting helpers.

**Triaged 2026-09-15: the defect is fixed; the lesson is now a standing rule.** The general
gap named above ("several tests exercise a helper rather than the path that uses it") was the
one R3 finding judged to generalise past its own theme, so it went into CLAUDE.md's
conventions as **"a test that supplies the argument cannot prove the caller supplies it"**,
with this failure as its evidence. The residual obligation — `render` having no end-to-end
test at all — is TS-2, deferred against 3c and planted in `cli.py::_run_render`'s docstring,
where whoever rewrites `render` cannot miss it.

---

## R3-3 — skipping unmappable entries, once every adapter can report a count

**Found during:** Theme 4, deciding VN-3.
**Files:** would touch all five adapters and `digest/errors.py`.

VN-3 was resolved as (a): one entry that cannot be mapped to an `Item` fails its whole feed,
with the entry's URL named in the message. (b) -- skip the entry, count it, raise only above a
threshold -- is more proportionate and was rejected for a structural reason, not a
preference. **Corrected wording (Theme 5):** all five adapters *have* a channel -- they
inherit `drain_notes` from the base. What `hn`, `hf_papers` and `gh_trending` lack is
anything to put in it: no `_notes` list, and nothing today that appends to one. So a skip
would be counted visibly in the two bundle adapters and silently in the other three. A silent drop in the places nobody can see is the exact
failure class this review exists to remove.

**Suggested trigger:** DUP-5 giving every adapter a *populated* notes channel — whenever it
lands, which is the third RSS bundle adapter, **not** Theme 5; Theme 5 ran on 2026-09-15 and
left DUP-5 deferred, so the parenthesis above named a session that came and went. Or the first
real mapping failure in production, whichever comes first. At that point (b) becomes strictly
better and the threshold needs naming; §8.1 Q1 applies, and the downstream measurement to
audit is `FeedOutcome.newest`, which feeds the staleness guard.

**Triaged 2026-09-15: deferred, trigger planted.** `errors.py::unmappable_entry`'s docstring
is where the decision is recorded and where the trade is explained, so the trigger sits next
to the code that would change. Its wording was also corrected there and in
`test_a_bad_datum_fails_the_feed_rather_than_vanishing`: all five adapters inherit
`drain_notes`, and three of them have nothing to put in it — the earlier "three adapters have
no channel" was the pre-Theme-5 description of the same fact.
