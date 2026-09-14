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
