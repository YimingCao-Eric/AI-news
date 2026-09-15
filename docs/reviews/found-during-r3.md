# Found during R3

Things noticed while applying a theme, outside that theme's scope. Not fixed. Recorded so
they are not rediscovered.

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
pushes their entries past `MAX_ENTRY_AGE_DAYS`, empties the source, and fails the `ai_blogs`
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

---

## R3-3 — skipping unmappable entries, once every adapter can report a count

**Found during:** Theme 4, deciding VN-3.
**Files:** would touch all five adapters and `digest/errors.py`.

VN-3 was resolved as (a): one entry that cannot be mapped to an `Item` fails its whole feed,
with the entry's URL named in the message. (b) -- skip the entry, count it, raise only above a
threshold -- is more proportionate and was rejected for a structural reason, not a
preference: only `arxiv` and `ai_blogs` have a `drain_notes` channel. `hn`, `hf_papers` and
`gh_trending` inherit the default returning `[]`, so a skip would be counted visibly in two
adapters and silently in three. A silent drop in the places nobody can see is the exact
failure class this review exists to remove.

**Suggested trigger:** DUP-5 giving every adapter a shared notes channel (Theme 5), or the
first real mapping failure in production -- whichever comes first. At that point (b) becomes
strictly better and the threshold needs naming; §8.1 Q1 applies, and the downstream
measurement to audit is `FeedOutcome.newest`, which feeds the staleness guard.
