# The interphase review, R0–R3

*Written 2026-09-15, after the last theme landed. Self-contained: everything you need to
understand what happened is in this file.*

---

## 1. What this was

Phase 3a added four source adapters to a pipeline whose shape had been decided while there
was exactly one. Nothing had yet asked whether that shape survived the other four. So before
phase 3b started changing the ranker, the whole codebase got reviewed along six dimensions —
silent failure and observability, layering and coupling, duplication and the adapter shape,
test-suite health, vocabulary and the error taxonomy, documentation drift — with every
finding sorted into one of three buckets:

- **A** — improvable within the project's stated constraints.
- **B** — a constraint itself causing real harm.
- **C** — the constraint working, and the obvious improvement being a regression.

**54 findings.** 43 fixed, 10 deferred against a stated trigger, 1 won't-fix. Five dimensions
produced no B at all and the sixth produced exactly one. Seven themes, applied one per
session, nine commits (`23d0b45` … `90bcaca`). The test suite went from 178 collected tests
to 242 — 150 test functions to 211 — and the pipeline snapshot, 425 rows, came out the far
end byte-identical. **Every fix-now decision was checked against the diff that implemented
it; five landed in a materially different shape than the triage specified**, all five in the
same direction, and section 9 says what they were.

---

## 2. The safety net came first

Before any review session ran, one commit built a characterisation snapshot: `23d0b45`.

It runs the real `fetch → store` composition over the recorded fixtures into a temporary
database and dumps every resulting row — url, title, source, published_at, dupe_of, and a
sha256 of the raw payload — sorted by url_hash. 425 rows, about 83 KB. It asserts nothing
about whether any of that is *correct*. It asserts only that it has not *moved*.

That is the point. Seven themes were about to rename things, split types, relocate a cap and
rewrite a registry, and the question after each one is "did I change behaviour I did not mean
to change?" Unit tests cannot answer that, because they test the parts. The snapshot covers
what 171 unit tests structurally could not: cross-source dedupe where url_hash cannot help,
the direction of every `dupe_of` relationship, URL canonicalisation across five sources at
once, four different date formats arriving at one store, and which items survive the
`ai_blogs` age cutoff.

**The clock is pinned from the fixture manifest, not from a constant** — and the reasoning is
the sharpest thing in the commit. `ai_blogs` filters entries against `now − 30 days`. Freeze
that clock at a hardcoded date and the moment fixtures are re-recorded, every entry falls
outside the cutoff. You get a snapshot of nothing, produced by a test that still passes. So
`record_fixtures.py` writes `captured_at` into `tests/fixtures/manifest.json` on every run,
and the snapshot reads it: refreshing the fixtures moves the clock as a side effect rather
than as something a human has to remember. The generator also refuses to proceed if
`ai_blogs` keeps *none* of its entries or *all* of them — either means the time window has
stopped being exercised and the snapshot would keep passing while protecting nothing.

**It is a contract, and regenerating it to make a test green is forbidden.** A snapshot
regenerated to silence a red test is a refactor with its evidence deleted — the file's whole
value is that it is the *previous* behaviour, and overwriting it converts a failed check into
a passed one without anybody learning what moved. Regeneration is legitimate only after the
behaviour change has been agreed in words first, and the commit message has to say what moved
and why.

Two findings surfaced while building it, both of which shaped later work:

- **Routing had to key on URL *path*, not full URL.** The HN request carries
  `created_at_i>{since_ts}` recomputed from the clock, so its full URL is never the same
  twice. The obvious exact-URL test harness would have failed intermittently.
- **The harness's own error had to derive from `BaseException`.** As a `RuntimeError`, a
  missing fixture route raised inside an adapter was caught by the pipeline's
  no-source-may-abort catch-all, filed as "source failed", and the generator carried on to
  emit a quietly *smaller* snapshot. A broken harness reported as a dead feed. This is the
  second independent discovery of that idiom, and the recurrence became the argument for
  giving it a shared base in Theme 4.

*Provenance: commit `23d0b45`; `scripts/snapshot_pipeline.py`, `tests/test_snapshot.py`.*

---

## 3. The one B — a quality gate that could not fail

`CLAUDE.md`, the working-rules file every session reads first, carried a Definition of
Done. It read, in full:

```
## Definition of done for any change
`uv run digest run --dry-run` completes without network errors being fatal, tests pass,
`ruff check` is clean.
```

`digest run` was a stub. It printed `not implemented`, printed the loaded config, and exited
0. It did that whether the adapters worked, whether they were all deleted, or whether the
database was missing. **A change breaking every adapter in the project satisfied the first
clause of the quality gate exactly as written.**

That is worse than having no clause. It reads like end-to-end verification, so it displaces
the check a session would otherwise have improvised. Nobody writes their own smoke test when
the project already tells them which one to run.

It was replaced with the instruments that actually exist:

```
uv run pytest                          # full suite; the conftest guard keeps it offline
uv run pytest tests/test_snapshot.py   # the composition is unchanged
uv run ruff check && uv run ruff format --check
```

plus the prohibition on regenerating the snapshot to go green. The `run --dry-run` clause is
scheduled to return in phase 3c with a real assertion about the file it writes.

This landed on its own commit rather than folded into a test refactor, deliberately: it is a
governance change, and burying it would hide it from whoever later asks why the gate moved.

*Provenance: commit `92c7e07`; the finding was logged as DD-1.*

---

## 4. The seven themes

### 4.1 Theme 1 — a suite that was going to expire on a calendar

**What was wrong.** Four `ai_blogs` tests were dated to fail with no code change whatsoever:
on 2026-09-21, when the OpenAI feed would cross its 7-day staleness threshold, and on
2026-10-01, when the Anthropic feed's newest entry would pass the 30-day ingest cutoff. The
adapter measured both against the live wall clock while reading fixtures frozen on
2026-09-14.

**The consequence.** A red suite is the least of it. A calendar failure is
*indistinguishable* from a real upstream death, and both produce the same response:
re-record the fixtures. Train that reflex on false alarms and the day a feed actually dies you
refresh straight past it. It also makes `git bisect` useless on anything older than a week.

**What changed.** The clock became a constructor-injected callable, pinned by the harness to
the fixture manifest's `captured_at`:

```python
# before
def _now() -> datetime:
    """The wall clock, behind one indirection so an offline harness can pin it."""
    return datetime.now(tz=UTC)


...


class AIBlogsAdapter(Adapter):
    def __init__(self) -> None:
        self._notes: list[str] = []

    async def fetch(self, client, source):
        now = _now()  # module-level patch point


# after
class AIBlogsAdapter(Adapter):
    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._notes: list[str] = []

    async def fetch(self, client, source):
        now = self._clock()
```

The naming split is deliberate and became a house rule: **a long-lived object takes
`clock: Callable[[], datetime]`, a single function call takes `now: datetime`.** Same concern,
two lifetimes, two names, so the difference is visible rather than discovered by type error.
The store already used `now=`; the adapter is constructed once and may fetch many times, so it
takes the callable.

**Three guards that certified nothing** were the more interesting half:

- `test_the_clock_is_pinned_to_the_fixture_set` asserted that the manifest file existed and
  that the clock function returned a timezone-aware datetime. **Both of those are satisfied by
  `return datetime(2026, 9, 14, tzinfo=UTC)`** — the exact hardcoded design the snapshot
  commit had explicitly rejected. The test certified the thing it existed to prevent.

```python
# before
def test_the_clock_is_pinned_to_the_fixture_set():
    manifest = REPO_ROOT / "tests" / "fixtures" / "manifest.json"
    assert manifest.exists()
    assert snapshot_now().tzinfo is not None


# after
def test_the_clock_follows_the_manifest_not_a_constant(tmp_path, monkeypatch):
    """Load-bearing: replace `snapshot_now`'s body with a constant and this must fail."""
    moved = tmp_path / "manifest.json"
    moved.write_text(json.dumps({"captured_at": "2031-07-04T12:34:56+00:00", "sources": []}))
    # ... assert snapshot_now() follows the file to 2031
```

- `test_recorded_feeds_are_all_currently_fresh` would have become *tautological* the instant
  the clock was pinned — fixtures are, by definition, fresh at the moment they were captured.
  It was replaced with a test of something that can actually be wrong: that each feed's
  configured staleness threshold is loose enough for that feed's own recorded publishing
  cadence. Its blind spot is one-sided and written into the docstring: it catches thresholds
  that are too tight, never ones that are too loose.
- **Nothing asserted the User-Agent was sent**, despite a real UA being a hard project
  constraint. The test transport ignores headers, so deleting `headers=` from the fetch loop
  broke no test. The first symptom would have been GitHub Trending returning 403 at 07:00 —
  the exact failure whose own error message tells you to check the User-Agent.

One more: `test_every_selector_is_a_module_constant` called `inspect.getsource` on two private
functions *by name*, so a behaviour-preserving rename failed it with `AttributeError`, while a
selector inlined into some third helper passed unnoticed. It now scans the module minus the
declared selector block — which mattered, because Themes 4 and 5 were about to rename things.

**Verification.** Run with the system clock faked to 2027-09-14: 22 tests pass. Snapshot
byte-identical. 181 collected tests, up from 178. The snapshot suite went from 6.8s of a 12s
run to a module-scoped fixture and 8.1s total.

*Provenance: commit `01b0d33`; findings TS-1, TS-3, TS-4, TS-5, TS-7, TS-8, VN-10.*

### 4.2 Theme 2 — one name doing two jobs, and a history that erased itself

**What was wrong.** Two findings, and they turned out to be the same finding.

`SourceHealth` was a single class serving both directions of the store boundary. The fetch
loop *wrote* it meaning "this run failed". `get_source_health` *read* it meaning "this source
has failed N times". The store recovered which was which by testing `last_success_at is not
None` — an inference that was correct only by accident of how one particular caller happened
to build the object.

Separately: a source that failed on odd days and succeeded on even ones read as **perfectly
healthy every single time you looked after a success**. `consecutive_failures` reset to 0 and
nothing else remembered anything.

**The consequence.** The break the first one invited is specific and plausible: a retry
wrapper, or a future `run` command, assembling a *complete* record by carrying the previous
`last_success_at` forward onto a failed run. An obviously sensible thing for someone to do.
It would have silently recorded a success and reset the failure counter to zero. And the
second means a feed that is dying slowly — up on Tuesday, down on Wednesday — never
accumulates evidence of it.

**What changed.** The type split into an event and a state, and the event makes the dangerous
record *unrepresentable*:

```python
class SourceOutcome(BaseModel):
    """What one run did to one source. An event, not a state."""

    name: str
    succeeded_at: datetime | None = None
    failed_at: datetime | None = None

    @model_validator(mode="after")
    def _exactly_one_instant(self) -> "SourceOutcome":
        if (self.succeeded_at is None) == (self.failed_at is None):
            raise ValueError(...)  # "failed, but here is when it last worked" cannot be said
```

`SourceOutcome` carries **no counts at all**. Not because counts are uninteresting, but
because the caller structurally cannot compute them — it cannot see the previous stored value.
Phase 1's fetch loop could only ever report 0 or 1 for exactly that reason. Removing the field
is what removes the temptation, and a test asserts the fields do not exist rather than
trusting a convention.

The store stopped guessing:

```python
# before
def record_source_health(conn, health: SourceHealth) -> None:
    """Success is inferred from `last_success_at is not None`, which is exactly how
    `fetch.py` builds the record. That coupling is implicit, hence this note."""
    if health.last_success_at is not None:
        ...


# after
def record_source_health(conn, name, *, succeeded_at=None, failed_at=None) -> None:
    if (succeeded_at is None) == (failed_at is None):
        raise StoreError(f"record_source_health({name!r}): pass exactly one ...")
```

A timestamp rather than a boolean, because the store needs to know *when* — `last_failure_at`
is half of what makes a recovered source's history survive, and a boolean could not supply it.
Taking `now()` inside the store instead would have planted a hidden clock there, days after
the project moved clocks to injection.

**The decision where two options were live: migration versus "delete the file".** The
`sources` table needed two new columns. Deleting and refetching is free today. It was rejected
because deleting discards accumulated `first_seen_at` history and stored raw payloads — which
exist precisely so the ranker can be re-run over weeks of history offline, and phase 3b is
about to change the ranker. Beyond that: once phase 5's scheduled job commits the database
back to the repo, it stops being disposable, and the first migration would then get written
under pressure instead of while the data is still cheap.

So schema 1 → 2 with real migrations, keyed by the version being upgraded *from*, applied in
an explicit transaction because the connection is in autocommit mode and a half-applied
migration is exactly the silent corruption the version check exists to prevent. A version
with no migration path still refuses rather than guessing.

And the test fixture for it, `tests/fixtures/schema_v1.sql`, is a **frozen copy extracted from
git history, never generated by current code**. A migration test that builds its "old"
database by calling today's `init_db` is migrating v2 to v2 within one release: green, and
proving nothing.

**Verification.** Run end to end against a real v1 database: 1 pre-existing item row, upgraded
in place through `digest fetch`, 429 rows afterwards, legacy row intact, both new columns
present. 185 tests. Snapshot unchanged — the dump is item rows only and never touches the
`sources` table.

*Provenance: commit `a757512`; findings LC-1, VN-6, SF-3.*

### 4.3 Theme 3 — a run nobody is watching

This is the theme with the highest-consequence finding in the review.

**What was wrong.** Every source could fail and the process still exited 0.

**The consequence, stated precisely.** `docs/PLAN.md`'s definition of done includes "runs
unattended for seven consecutive days with no manual intervention." Seven consecutive *total
failures* would have satisfied that criterion while delivering nothing, and a scheduled job
would have stayed green throughout. You would have concluded the project was finished.

Five more findings in the same family:

- The one-line-per-source run log that `CLAUDE.md` *mandates* sat at INFO under a
  WARNING default. **The 07:00 scheduled run wrote it nowhere.** Nobody passes `-v` in a
  crontab. That line is the one artefact that would show a source quietly returning zero for a
  week.
- `digest render` created the database it was supposed to read. A typo'd `--db` produced a
  brand-new empty file and a confident "Nothing new" — the failure and the success printing
  the same words.
- The health footer had one shape for several distinct states, so "ran and returned nothing"
  and "did not run at all" were indistinguishable.
- arXiv's announce-type filter could drop 100% of its input and report success.
- The per-source duration was read with `getattr(result, "durations", {})`, so a rename would
  have reported `duration=0.00s` forever.

**What changed.**

```python
def _exit_code_for(result: FetchResult) -> int:
    """0 unless *every* enabled source failed.

    Deliberately reads the outcomes rather than touching how failures are caught.
    """
    if not result.outcomes:
        return EXIT_OK
    if all(not outcome.succeeded for outcome in result.outcomes):
        return EXIT_ALL_SOURCES_FAILED
    return EXIT_OK
```

The codes: `0` ran, `2` usage or configuration error, `3` storage error, `4` every enabled
source failed, `130` interrupted, and later `70` for an internal error. **Partial failure
stays 0 on purpose** — sources fail routinely, and a code that fires most mornings gets
filtered into a folder nobody opens.

Two of those numbers were settled by measurement rather than assumption. `digest --nonsense`
already exited **2**, because argparse hardcodes 2 in `parser.error()` and that is not ours to
reassign; `digest` with no subcommand exited 1. Two spellings of "the invocation is wrong"
returning different codes is a distinction nothing can use, so 2 was widened to mean usage
*or* config — both are "a human must fix this, retrying will not help", which is the only
distinction a scheduler can act on. 130 is 128+SIGINT, which cron and CI already understand.

The database stopped inventing itself:

```python
# before
conn = store.init_db(_db_path(args))

# after
# `create=False`: sqlite3 will happily invent a database for any path, so a typo'd
# --db used to yield a brand-new file and a confident "Nothing new" -- the failure and
# the success printing the same thing.
conn = store.init_db(_db_path(args), create=False)
```

The footer grew five distinguishable states — `ok`, `quiet`, `FAILED`, `disabled`, and the
indented per-feed `STALE` notes under a bundle — and `disabled` suppresses its counts with a
dash rather than printing `0`, because there are none rather than zero of them.

The neatest piece of reasoning in the theme is the `≥` marker. Theme 2's migration back-fills
`total_failures` as 0 for pre-existing rows, so a long-failing source could print
`consecutive=2 total=0` — a self-contradiction that *under-reports* a health signal. Rather
than seeding a plausible lie or adding a column to track provenance, the footer leans on an
invariant that already holds: `total ≥ consecutive` for any row counted since v2, so a row
where it is *less* provably predates counting, and prints `total=≥2`. It states a floor
instead of inventing a number, it never over-claims, and **the marker disappears by itself**
once the true count overtakes. The one case no scheme recovers — fifty pre-v2 failures
followed by a recovery, which reads as `total=0` because nothing anywhere remembers — is
written into the docstring rather than papered over.

The arXiv filter guard is the same shape as a hazard found in phase 3a:

```python
# after
if announced == 0 and failures == 0:
    raise RuntimeError(
        f"{source.name}: all {len(feeds)} categories parsed cleanly and "
        f"announced zero entries between them. ..."
    )
...
# and the notes carry the pre-filter denominator
self._notes.append(f"{feed.name}: {len(kept)} new/cross of {total_entries} entries")
```

**"0 of 0" is a quiet day. "0 of 270" is a broken filter.** Without the denominator both
print as a bare zero. The filter keys on a string field, so arXiv renaming `new` or `cross`
would drop 100% of input while the source reported success. The same all-empty gap existed in
the other bundle adapter and was closed there too — counting *entries* rather than items on
purpose, so feeds full of entries that are merely too old still report STALE instead of
raising.

**Verification.** 202 tests. Snapshot watched specifically across the arXiv guard, which was
the one change in the theme that could have moved it. Unchanged.

*Provenance: commit `c487942`; findings SF-1, SF-2, SF-6, SF-7, SF-8, SF-9.*

### 4.4 Theme 4 — our bug or their outage?

**What was wrong.** Seven deliberate raises across the adapters used `ValueError`,
`TypeError`, `RuntimeError`, `LookupError` and one bespoke `GitHubTrendingError` — all
flattened by the fetch loop into `f"{type(exc).__name__}: {exc}"`.

**The consequence.** A deliberate `TypeError` meaning "the API returned a dict, not a list"
and an accidental `TypeError` from a coding mistake produced the same health record, the same
footer line, and the same log shape. The first means *re-record a fixture*. The second means
*revert a commit*. A coding mistake filed itself as a dead feed and cost you a morning
checking a healthy source.

**What changed.** A new `digest/errors.py` holding two axes, deliberately separate:

- `AdapterError(RuntimeError)` — a source cannot be used, and the adapter **anticipated** it.
  Subtypes: `SourcePayloadError` (wrong shape, unparseable, zero where zero is impossible),
  `SourceBlockedError` (403 or rate limit — check the User-Agent, back off),
  `NoAdapterRegistered`.
- `DigestControlError(BaseException)` — the program's assumptions about its own *execution*
  are violated. Outside `Exception` on purpose.

**The decision that mattered most was what *not* to do.** The obvious move is to narrow the
catch-all to the new named base. That was refused:

```python
# unchanged, and deliberately so
except Exception as exc:
    error = f"{type(exc).__name__}: {exc}"
    expected = isinstance(exc, AdapterError)
    if expected:
        log.warning("source %s failed: %s", source.name, error)
    else:
        log.error("source %s CRASHED (this is a bug in the adapter, not the source): %s",
                  source.name, error, exc_info=True)
```

Narrowing would let an accidental `TypeError` escape the handler and bypass both the health
record and the `finally` notes drain — which is the exact failure the never-abort rule exists
to prevent. **The named base classifies what was caught; it never changes what is caught.** A
test asserts a crashing adapter still leaves the run intact with the other sources
contributing.

The classification reaches the run log as `status=failed` versus `status=CRASHED`, the latter
with a traceback. It is deliberately **not** persisted — "which runs crashed" is run
provenance, which is a deferred finding against phase 4's runs table. The test that owes it
says so in its own name, so nobody goes hunting for a column that was never added.

`DigestControlError` gave the `BaseException` idiom the shared base it had been missing.
Both of its subclasses — the test-suite network guard and the snapshot harness error — had
been discovered *independently*, each by a test failing with the wrong message. Two
independent discoveries of one idiom is the argument for naming it. Its home is the `digest`
package because tests and scripts cannot import each other but both already import `digest`.
It maps to exit 70 (EX_SOFTWARE) rather than falling into 4: **4 means the environment failed
and tomorrow may work, 70 means retrying cannot help.**

Errors also started naming the datum that broke:

```python
def unmappable_entry(source_name, feed_name, identifier, cause) -> SourcePayloadError:
    """One unparseable date used to fail a 270-entry feed with a message naming neither
    the entry nor its URL."""
    return SourcePayloadError(
        f"{where}: cannot map entry {identifier} to an Item -- {cause}. "
        f"One malformed entry fails this feed rather than being dropped silently; "
        f"the identifier above is the one to look at."
    )
```

You could not tell which of 270 entries to look at — and if the bad entry came from a live
feed rather than a fixture, it was gone by the time you looked.

**Whether to skip the bad entry instead of failing the feed** was live, and was decided
against on a structural ground rather than a preference. Skipping and counting is more
proportionate, but only the two bundle adapters have anywhere to *report* the count: all five
inherit the notes channel, and three of them keep no notes list and append to nothing. So a
skip would be counted visibly in two places and silently in three — a silent drop in exactly
the spots nobody can see, which is the failure class this whole review exists to remove. Loud
and disproportionate beats quiet and proportionate, until every adapter has a populated
channel.

Before choosing `RuntimeError` as the base, it was checked that nothing in the source, tests
or scripts catches `RuntimeError` or uses a bare `except` — so the new hierarchy widens no
existing handler.

**Verification.** 217 tests. Snapshot unchanged — and verified beforehand that **0 of 2305
recorded fixture entries fail `Item` construction**, so neither branch of the skip-versus-fail
decision could have moved it either way.

*Provenance: commit `3432881`; findings VN-3, VN-4, VN-5, VN-8.*

### 4.5 Theme 5 — which implementation, versus which source

**What was wrong.** The adapter registry held one long-lived *instance* per *source name*.
That conflated two different questions: which implementation to use, and which source it
serves.

**The consequence.** One class could therefore serve exactly one source. A second RSS source
wanting identical behaviour had two options: write a subclass whose only content is a
different name, or register the same instance under two names. The second is the obvious
shortcut — and it means two concurrently-fetched sources share one `self._notes` list.
Whichever drains first gets a mixture, the footer shows one source's per-feed diagnostics
under the other source's name, **nothing raises, and no test fails.** `docs/PLAN.md`'s
source catalogue lists a Tier 2 that is mostly more RSS, so this was queued to happen
roughly ten times.

A related finding: `kind` was declared on every source, printed in the config summary, and
dispatched on nowhere. Dead config that looks load-bearing.

**What changed.** `kind` became the dispatch key and the registry started holding classes:

```python
# before
ADAPTERS: dict[str, Adapter] = {
    adapter.name: adapter
    for adapter in (HNAdapter(), GhTrendingAdapter(), ...)  # instances, keyed by source
}

# after
IMPLEMENTATIONS: dict[str, type[Adapter]] = {
    cls.kind: cls
    for cls in (HNAdapter, GhTrendingAdapter, ...)  # classes, keyed by kind
}
KNOWN_KINDS: frozenset[str] = frozenset(IMPLEMENTATIONS)
```

`fetch_all` now constructs **one instance per source per run**, and the notes drain takes the
instance it fetched with rather than looking it up again — so per-run adapter state is private
*by construction* instead of by an invariant living in a docstring.

The old `kind` values could not have become the dispatch key, and this is a precondition
rather than a preference: they were `json_api`, `rss` and `html`, where `json_api` covered
two unrelated adapters and `rss` covered two more. Four sources, two values, four different
implementations. The new values are `hn`, `gh_trending`, `hf_papers`, `ai_blogs`, `arxiv`.
They coincide with the source names because each implementation serves exactly one source
today, and **that redundancy was left visible rather than hidden behind a clever generic
value** — the first divergence is a second RSS source declaring an existing kind, which is
precisely the moment to rename the implementation.

`kind` deliberately never encodes *shape*. Whether a source has one endpoint or six is still
decided by field presence — `url` versus `feeds` — and normalised so adapters iterate and
never ask how many. That phase 0 decision is untouched.

Validation moved to config load:

```python
def load_sources(config_dir=None, *, known_kinds: frozenset[str]) -> SourcesConfig:
    ...
    for source in config.sources:
        if source.kind not in known_kinds:
            raise ConfigError(
                f"{path}: source {source.name!r} has kind {source.kind!r}, "
                f"which no adapter implements. Valid kinds: ..."
            )
```

A typo now fails when the file loads, naming the valid set — rather than at fetch time, by
which point four healthy sources have been fetched and the fifth looks like an outage.

**`known_kinds` is injected and required, and there was a real argument here.** Injected
because the config module cannot import the registry: the adapter base imports `Source` from
config, so reaching back is a cycle. The alternative — a second frozenset literal in the
config module, pinned to the registry by a test — is two sources of truth for one fact.
Required rather than optional because a caller who forgets should get a `TypeError`, not
silently unvalidated config. This is the third place the same injection pattern now appears,
after the clock and the adapters themselves; a fourth mechanism for the same concern is the
shape a vocabulary finding had already flagged once.

One deliberate detail: `fetch_all(adapters=...)` **merges** rather than replaces, so naming
one source leaves the other four constructing normally. That let the snapshot harness pin the
`ai_blogs` clock through the public API instead of assigning into a module global, and eight
test sites stopped mutating globals.

**Verification.** 228 tests. Snapshot unchanged — this theme changes *who constructs* the
adapter, never what it does once constructed. Both CLI paths exercised this time, explicitly
because a Theme 3 edit had been missed by checking only one: `digest fetch` (5 sources, 428
items, exit 0), `digest render` (428 items, footer, exit 0), the stub paths, and a typo'd
`kind` failing at load with exit 2.

*Provenance: commit `57c5d59`; findings LC-2, LC-4, LC-9.*

### 4.6 Theme 6 — one invariant, one place; one word, one meaning

**What was wrong.** Nine findings, in two groups.

*Duplication.* A date-conversion helper was byte-identical in two adapters. An ISO-with-`Z`
parser was duplicated between the two JSON adapters. Title whitespace normalisation existed
in four adapters and not in the fifth. The `fetch_limit` slice was copied four times — and in
the fifth adapter it meant something else entirely.

*Vocabulary.* "New" meant two different things in adjacent user-visible output. `upsert_items`
did not upsert. Three constants named "how far back" in two units. The HN points floor was
stated in two config files with nothing reconciling them.

**The consequences.** The duplicated date helper enforces the rule that has held since phase
0 — the item model rejects naive datetimes — so a correction had to be made in two files, and
whichever copy was missed would fail *for one source only*, reading as "that feed is
malformed" rather than "we fixed this in the wrong file." The tempting response when that
validator fires is to relax the validator, which would undo phase 0.

The `fetch_limit` one is sharper. Four adapters enforced it as a post-condition by slicing
their output. `hn` set Algolia's `hitsPerPage` and trusted the server — **a request hint doing
a guarantee's job.** Drop that parameter in a URL edit, or meet a server that ignores it, and
`hn` silently exceeds its configured limit while the other four structurally cannot.

```python
def _apply_fetch_limit(items: list[Item], source: Source) -> list[Item]:
    """Enforce `fetch_limit` once, here, for every adapter."""
    if source.fetch_limit is None:
        return items
    return items[: source.fetch_limit]
```

`hn` keeps `hitsPerPage` as the optimisation it always was — do not download a thousand to
keep thirty — but it is no longer the guarantee. Order is preserved, so arXiv's round-robin
interleave still decides *which* items a binding cap keeps, fairly across categories rather
than alphabetically.

The points floor failed asymmetrically, which is why it was worth fixing: `points>100` lived
in `sources.yaml` and `min_hn_points: 100` in `interests.yaml`, reconciled by a comment. *Raising*
the floor worked. *Lowering* it changed nothing — the server-side filter still cut at the old
value, so those stories never entered the database to be re-ranked, and nothing anywhere said
so. It is now derived: the URL carries `{min_points}` and config load resolves it, so there is
no second value to drift, and a placeholder still wearing braces at request time is refused
rather than shipped.

Extracting the ISO parser **nearly introduced a silent degradation, and a test caught it**:

```python
def parse_iso_utc(value: str | None) -> datetime | None:
    """Raises on a present-but-unparseable string rather than returning None.

    Absent and malformed are different. Absent means the source did not say, and an item
    with no date is a state the model supports. Malformed means the source said something
    we no longer understand, and quietly turning that into "no date" strips the item's
    recency silently -- the item survives, so nothing looks dropped, and it simply never
    ranks.
    """
```

The first version returned `None` on a parse failure, so that a caller could fall through to
a second field. Reasonable-looking, and it would have converted every malformed date into an
item that quietly never ranks.

On vocabulary: the three windows now name their *stage* — `REQUEST_WINDOW_HOURS` (the request;
old items never arrive), `INGEST_MAX_AGE_DAYS` (after parsing; old entries arrive but are not
stored), `max_age_hours` (selection). They all happen to be 48 or 30 today and they are **not
the same concept**; collapsing them would undo the phase-1 slow-burner fix, which exists
because a story posted 30 hours ago that only crosses 100 points this morning was never inside
a 24-hour window on either run. Naming only — the concepts stay separable.

"New" split into `inserted=` in the log (rows written this run) and "not yet in a digest" in
the render output (items never shown), with `store.new_items` renamed `unrendered_items`. The
two counts routinely disagree while both are correct, so they must not share a word.
`upsert_items` became `insert_items`.

One packaging finding: `scripts/` imported pytest, and `tests/` imported `scripts/`. A phase 5
CI running a sync without dev extras would have broken the snapshot script while tests passed
locally. Now stdlib mock, and `scripts/` is a declared package — verified by importing it with
pytest blocked, and it still generates the full 83 KB snapshot.

**Verification.** 242 tests. Snapshot unchanged, and for a reason measured *before* the
change rather than rationalised after: zero of the 33 recorded HN titles contain a tab,
newline or double space, so title normalisation was a guard rather than a repair, and any
movement would have been a regression rather than a cosmetic diff. All four CLI paths
exercised.

*Provenance: commit `4f19f63`; findings DUP-2, DUP-3, DUP-4, DUP-6, LC-5, LC-7, LC-8, VN-1,
VN-2, VN-9.*

### 4.7 Theme 7 — the documents, last, because six themes had invalidated them

**What was wrong.** The README described a phase-0 skeleton in which the pipeline stages were
stubs. The adapter contract file — the first thing the author of a sixth adapter reads — named
the wrong module as owner of the run log, described `kind` as "free-form text" two commits
after `kind` became the required dispatch key, and referenced a type that Theme 2 had split in
half. Seven separate drifts in `docs/PLAN.md`, including a source volume recorded as "50
items" that had been measured at 270 two days later, an instruction to use a HTML parser the
fixed stack in `CLAUDE.md` forbids, a "Done when" clause naming a CLI flag that never existed,
and a hard constraint — "back off on 429" — with no implementation anywhere.

**The consequence is not aesthetic.** A hard constraint reads as a *description of current
behaviour*, so the next person builds on it. A documented parser choice that contradicts the
fixed stack would have you install a forbidden dependency. And a count without a date reads as
a constant: the "50 items" figure was recorded once, and the next person to see 180 on a
Sunday would think something was broken.

**The rule this theme settled**, because the guard given to it was correct but not yet usable
by someone who had not been in the conversation:

> **A record of intent gets a dated amendment. A catalogue of measurements gets a correction.**

Per-phase "Done when" lines are records of intent — in a project whose recurring bug class is
two individually-correct decisions colliding, the gap between what was believed and what
shipped is the most informative thing on the page. Overwriting it destroys the only evidence
that the gap existed. So phase 1's `digest fetch --source hn` clause stays, with a dated note
explaining that the flag never existed and that which sources run is config rather than an
argument. Phase 2's "outputs only items first seen today" stays, with a note recording the two
concrete reasons it was replaced during the phase: timestamps are UTC while the digest day is
Vancouver local, so a 07:00 run splits one morning across two UTC days, and a missed run would
silently lose a day instead of catching up.

Source volumes are measurements, so they were corrected — and dated on both sides. The plan
now records 2026-09-12 cs.AI at 50 items, 2026-09-14 at 270 with cs.CL 115 and cs.MA 16, and
says explicitly that roughly 38% of that 270 are revisions rather than new work, that the
figure swings by weekday and category, and that the thing indicating a fault is not a low
count but `0 new/cross of 270`.

The adapter contract and the README are neither kind of record — they describe what is true
now — so both were rewritten.

**Two tripwires were planted**, on the grounds that prose describing code drifts the moment
the code moves: a comment beside the exit-code constants and beside the footer function saying
these are published in the README and must move together. Cheaper than a test, and it fires at
the moment of the change rather than at the next review.

Every deferred finding's trigger was also planted where the work would actually be done — a
docstring, a constant, a config comment — rather than living only in a triage table that fires
only if someone rereads it. Section 6 lists all eleven with their locations.

**Verification.** Every command `README.md` and `CLAUDE.md` claim was executed
verbatim. That found three more instances of the same defect class the theme exists to fix: a
snapshot regeneration flag (`--write`) that does not exist, an illustrative footer that said
"5 of 5 sources enabled" while showing one source as disabled, and — in newly written README
prose — a reference to the very `--source` flag the theme was correcting elsewhere. All three
fixed before the commit. 242 tests, snapshot unchanged.

*Provenance: commit `90bcaca`; findings DD-2 through DD-11.*

---

## 5. What was found *while* fixing

These are not in any of the six reports. They exist only in the commits, and they are the
material that shows the review working rather than being performed.

**The fixture recorder hardcoded the HN URL in triplicate.** Deriving the points floor from
`interests.yaml` put a placeholder on a path the recorder bypassed — and the recorder was
worse than predicted. It did not just duplicate the points floor: it hardcoded the entire
request URL, restating the floor, the 48-hour window *and* the page size. Change any of them
in config and the recorder kept capturing a window production no longer used, so the fixtures
and the pipeline would have silently disagreed — and the snapshot, the contract every theme
was measured against, would have been built from them. It now builds the request through the
adapter's own function, which also puts it behind the guard that refuses to ship an unresolved
brace to the API.

**A helper that would have turned malformed dates into absent ones.** Covered above, and
worth repeating as a pattern: the degradation was introduced by a *refactor*, in the first
version of an extracted helper, for a sensible-sounding reason (let the caller try a second
field). What caught it was a test asserting that a bad datum fails loudly — a test that
existed because of a *different* finding in the same theme.

**A Theme 3 edit that silently never applied.** The footer change had two call sites. The
scripted edit for the second did not match, so `digest render` kept calling the footer without
the argument that distinguishes a disabled source from a quiet one — and printed `quiet` for
disabled sources for a whole theme. **No test caught it, because the test called the footer
function directly with the argument supplied.** It tested the function, not the call site. It
was found in Theme 4 while reading that file for something else, fixed there, and disclosed
rather than buried. It is now a standing rule in `CLAUDE.md`: *a test
that supplies the argument cannot prove the caller supplies it* — when a change adds a
parameter or a call site, at least one test must drive the entry point.

**The exit-code collision with argparse.** The plan was for 1 to mean "config error". Checking
rather than assuming showed that argparse hardcodes 2 in `parser.error()`, so `digest
--nonsense` already exited 2 while bare `digest` exited 1 — two spellings of the same
condition returning different numbers. 2 was widened to cover both. Had this been assumed
instead of measured, a scheduler distinguishing 1 from 2 would have been distinguishing
nothing.

**A filter that would have blinded the measurement behind it.** From phase 3a, and it became
one of the review questions the project now applies to every change: a 30-day ingest cutoff
and a staleness warning were both individually correct, and composing them would have blinded
the warning *precisely on the feeds it existed to catch* — a 112-day-stale feed has zero
entries inside a 30-day window, which reads as a quiet week. Fixed by measuring "newest entry"
over every entry *before* the cutoff. The generalised question is now asked routinely: for
every filter, what downstream measurement reads the filtered data, and does it need the
unfiltered version?

**A bare `ValueError` escaping the wrapper it was supposed to be inside.** Writing the test
for "errors must name the bad datum" found a real gap in the same change: the HN date parse
raised `ValueError` from `fromisoformat` *before* the model saw it, so it escaped the wrapper
that names the entry. The catch was widened to `ValueError`, which subsumes the model's
validation error.

**A snapshot harness error that was being filed as a dead feed.** Covered in section 2 — and
the reason it matters is that it happened *twice*, independently, with the test-suite network
guard and then the snapshot harness. Both were `Exception` subclasses, both were swallowed by
the deliberately broad catch that exists so no source can abort a run, and in both cases the
symptom was a test that passed while protecting nothing. The recurrence is why Theme 4 gave
the idiom a shared, findable base rather than leaving it as two coincidences.

**Documentation that does not run.** Executing every command `README.md` and `CLAUDE.md`
claim, as the last act of the last theme, found three claims that were false — including
one written in that same commit. The point generalises: an instruction that looks like a check
and is not is the same defect class as the Definition of Done that could not fail.

---

## 6. What was *not* fixed

Ten findings were deferred, each against a condition rather than a mood, and each one's
trigger is now written into the file where the work would actually be done. One finding was
refused outright.

| Deferred | What it is | Why deferred | Trigger | Trigger written in |
|---|---|---|---|---|
| **DUP-1, DUP-5** | The bundle fan-out — fetch N feeds, gather, classify results, drain notes — exists twice, in the two bundle adapters, and the copies have drifted | Extracting over two implementations means guessing the hook signature while the two disagree about what a per-feed result even is. The all-feeds-empty gap in *both* copies was fixed anyway, under Theme 3 | The third RSS bundle adapter | `adapters/_mapping.py` module docstring |
| **LC-3** | The pipeline's composition lives inside an argparse handler rather than an orchestrator | Two stages fit in a handler. Building the orchestrator now means rewiring it when the third stage arrives | Phase 3c, composing the third stage | `cli.py::_run_fetch` docstring |
| **SF-4, SF-5** | A run that inserts nothing leaves no trace it happened, and per-feed notes are printed once and never stored | Both want a `runs` table, which phase 4 brings for cost logging anyway. Building one here means building it twice | Phase 4. "Did Tuesday's job run?" must be answerable before phase 5 ships | `models.py::SourceOutcome.expected_failure` |
| **TS-6** | No test exercises a timeout actually expiring — the suite proves the budget is configured, never that exceeding it is recorded as a failure | A timeout that silently stopped firing looks exactly like a fast morning, but nothing in the review suggested it had | Next change to timeout handling, or the third bundle adapter | `fetch.py::SOURCE_TIMEOUT_SECONDS` |
| **TS-2** | `digest render` has no tests at all | Phase 3c rewrites it into a jinja2 renderer. Tests written now test code about to be deleted | Phase 3c — and at least one must drive the CLI entry point, not the helpers | `cli.py::_run_render` docstring |
| **VN-7** | Three levels of result (`FetchResult` per run, `SourceOutcome` per source, `FeedOutcome` per feed) across two words, with the innermost level in one adapter still an untyped tuple | Settling a naming scheme over three instances is a rename with an obvious answer; over two it is a guess | A fourth result type appears | `models.py::SourceOutcome.expected_failure` |
| **DD-3** | "Back off on 429" is a hard constraint with no implementation. A 429 raises and that source is lost for the day — correct, but not backoff | The wording was fixed immediately (it now sits in an explicit "not yet implemented" table, because an unmarked constraint reads as a description of current behaviour). Only the behaviour is deferred | The first 429 actually seen, or adding Reddit, which throttles hard and is next on the source list | `CLAUDE.md`, "Not yet implemented" |
| **DD-7** | The plan mandates conditional GET on every RSS source. It is half-built in one adapter — headers and the 304 path are real and tested — but no validators are stored, so every request goes out unconditional, and the other RSS adapter does not do it at all | Storing validators needs a `feed_state` table: the `sources` row has one etag column and that bundle has six feeds. The payoff is politeness, not speed, on six small files fetched once a day | A host rate-limiting us | `docs/PLAN.md` section 3; `ai_blogs.conditional_headers`; and the name of the test that pins it |
| **LC-6** | The ingest window is hardcoded and unaware of the selection window that governs it | The assertion belongs where the selection window is first read, which phase 3b builds | Phase 3b, which already owed it | `adapters/hn.py::REQUEST_WINDOW_HOURS` |
| **R3-1** | One `captured_at` covers the whole fixture set, so refreshing a single source restamps the clock for all thirteen fixtures | Fixing it changes the manifest format, which the snapshot reads — and the theme that found it was forbidden from moving the snapshot | Any edit to the recorder. Until then, refresh everything at once | `scripts/record_fixtures.py::write_manifest` |
| **R3-3** | Skip-and-count unmappable entries instead of failing the whole feed | Only two of five adapters have anywhere to report the count, so a skip would be silent in three of them | The shared notes channel landing, or the first real mapping failure | `errors.py::unmappable_entry` |

That list is eleven rows for ten deferred findings plus one deferral discovered during R3;
R3-1 and R3-3 came out of the work rather than the reports, and DD-7 was promoted from
*invisible* to *deferred* — it had been a silent obligation nobody had recorded as unfinished.

**Won't fix — one item.** `test_raw_contains_no_clock` asserts an exact key set on a stored
payload, which is brittle by any normal standard: add a harmless field to the raw dict and it
fails. It stays exactly as it is, because the strictness *is* the point. It is the regression
test for a real bug where an adapter wrote `scraped_at=datetime.now()` into the payload it
stored — nondeterministic, and a layering violation, and it would have made every snapshot
run produce a different file. Loosening the assertion to "the important keys are present"
would let the next clock back in.

---

## 7. The C findings — where the obvious fix is a regression

Six places where something looks wrong, an improvement is obvious, and the improvement is the
bug. This is the section that explains why the codebase looks the way it does.

**Do not narrow the catch-all.** The fetch loop catches bare `Exception` around every adapter,
which looks like the anti-pattern it usually is. Narrowing it to the named adapter-error base
— the obvious move once that base exists — would let an accidental `TypeError` escape,
bypassing both the health record and the notes drain, and killing the run. One dead feed must
never abort the digest. The base classifies; it never narrows.

**Do not make partial failure non-zero.** Three of five sources dying looks like something a
scheduler should hear about. But sources fail routinely, and an exit code that fires most
mornings gets filtered into a folder nobody reads — at which point it cannot signal the
morning that matters. Partial failure is carried by the footer and the per-source log line,
both of which were made visible in the same theme. Only *total* failure changes the exit code.

**Do not fold the arXiv identifier into URL canonicalisation.** Two sources carry the same
paper under different URLs that both end in the same arXiv ID, so canonicalising on the ID
would collapse them elegantly. It would also collapse them through `INSERT OR IGNORE`: no
row, no log, and the only symptom is an item you never knew existed. Near-duplicates are
stored and *flagged* instead, keeping both rows and recording the relationship.

**Do not strip `source` from URLs.** It looks like a tracking parameter. A false *split* shows
you a story twice and you shrug; a false *collapse* drops an item invisibly. The asymmetry
decides it.

**Do not exempt four-digit years from the numeric duplicate guard.** Requiring identical
numeric tokens means "at CES 2027" and "at CES" read as different stories, which is a false
negative — you see the item twice. Exempting years fixes that harmless case and buys back a
dangerous one: "CES 2026" and "CES 2027" really are different events.

**Do not merge the three 48s.** Three constants share a value and look like duplication. They
are the request window, the ingest window and the selection window, and they are meant to be
able to diverge — fetching 72 hours while selecting 48 is a reasonable thing to want.
Collapsing them silently undoes the fix that exists because a story which only crosses the
points floor on its second day was never inside a 24-hour window on either run.

The counters living in the store rather than travelling on the outcome object belongs here
too: it looks like business logic in the persistence layer, and it is the only place that can
see the previous value.

---

## 8. Where the reports and the diffs disagree

Every fix-now decision was checked against the commit that implemented it. **The findings and
the fix/defer decisions held everywhere.** No finding was quietly dropped, no deferral quietly
became a fix, and no "won't fix" was reversed. What did move, in five cases, is the
*mechanism* the triage proposed — and in every one of the five the implementation is more
conservative or more specific than what was written before the code was touched.

| Triage said | What landed | Why |
|---|---|---|
| Convert the clock to parameter injection (`now=`), since the store already does it | Constructor injection, `clock: Callable[[], datetime]` | The store's `now=` is a single call at one instant; the adapter is constructed once and fetches many times. Two lifetimes, so two idioms, named apart on purpose |
| `record_source_health(conn, name, *, succeeded_at: datetime \| None)` | Both `succeeded_at` and `failed_at`, with exactly one required | A single nullable parameter still encodes the outcome as a presence test — the same inference the finding was about. Two parameters and a validator make the bad record unrepresentable |
| Key the registry by source name and construct one instance per source | Keyed by `kind`; one instance per source | Keying by source name preserves the conflation the finding names. Keying by kind is what makes one implementation able to serve two sources at all |
| Rename `upsert_items` to `insert_new_items` | `insert_items` | `insert_new_items` reuses the exact word the adjacent finding was removing from the vocabulary |
| Mark stubs and add `--db` to the README | That, plus an exit-code table and the footer's five states | Neither was documented anywhere, and both are what a scheduler and a half-awake reader actually need |

One correction also ran the other way. A note written during Theme 4 said three of five
adapters "have no notes channel". Theme 5 established that all five *inherit* the channel;
three of them simply have nothing to put in it. The finding is unchanged — a skip would still
be silent in three places — but the wording was wrong in three files and was corrected in the
last theme.

---

## 9. What this review did not examine

Six dimensions were chosen, and they are not the whole space. **Nothing here looked at
performance, concurrency correctness beyond "one source must not abort the run", data growth
over a year, database size or query plans as the item table grows, or the dependency supply
chain.** There is no finding about what happens when the store holds 150,000 rows, because
nobody looked; the absence of performance findings is evidence about the review's scope, not
about the system's performance. The same goes for anything security-adjacent, and for the
LLM-facing design that phase 4 will introduce and that did not exist to review. "54 findings,
all decided" is complete only within the six dimensions named at the top.

---

## 10. Verification summary

| Stage | Commit | Collected tests | Snapshot |
|---|---|---|---|
| R0 — snapshot built | `23d0b45` | 178 | created, 425 rows |
| R1/R2 — reports and triage | `8a7a1ed` | 178 | unchanged |
| Definition of Done | `92c7e07` | 178 | unchanged |
| Theme 1 — test determinism | `01b0d33` | 181 | unchanged |
| Theme 2 — health split, schema v2 | `a757512` | 185 | unchanged |
| Theme 3 — unattended signals | `c487942` | 202 | unchanged |
| Theme 4 — error taxonomy | `3432881` | 217 | unchanged |
| Theme 5 — dispatch on kind | `57c5d59` | 228 | unchanged |
| Theme 6 — shared helpers, vocabulary | `4f19f63` | 242 | unchanged |
| Theme 7 — documentation | `90bcaca` | 242 | unchanged |

Test functions went from 150 to 211; the collected count is higher because of parametrisation.
`ruff check` and `ruff format --check` clean at every commit.

**The snapshot never moved.** Not once, across seven themes that split a model, renamed six
things, relocated a cap, replaced a registry and rewrote an error hierarchy. Where a change
*could* have moved it, that was checked deliberately rather than noticed afterwards: Theme 3
watched it across the arXiv guard specifically, and Theme 4 confirmed in advance that 0 of
2305 recorded fixture entries fail item construction, so neither branch of its
skip-versus-fail decision could have changed a row.

**CLI paths exercised.** All four subcommands, run for real against the live sources at the
end: `digest fetch` (459 fetched, 428 inserted, 4 duplicates, exit 0), `digest render` (428
items with footer, exit 0), and the two stubs. Flags: `--dry-run`, `--db`, `--config-dir`,
`-v`. Exit codes observed directly from the CLI: 0 on success, 2 for a bad flag, for a missing
subcommand and for a bad config directory, 3 for a database that does not exist. 4, 70 and 130
cannot be provoked from a prompt and are covered by tests that pin which situation produces
which constant and that the codes stay distinct.

The per-source run log was confirmed to appear on stderr *without* `-v`, which was the whole
point of one of the findings — five lines, one per source, at the default log level.

*Provenance for this document: the six dimension reports and `triage.md` alongside this
file, plus `found-during-r3.md`, for what was found; `git show` on `23d0b45`, `8a7a1ed`,
`92c7e07`, `01b0d33`,
`a757512`, `c487942`, `3432881`, `57c5d59`, `4f19f63`, `90bcaca` for what changed.*
