# Review: silent failure and observability

Interphase review, dimension 1 of N. Reviewed against `CLAUDE.md` and `docs/PLAN.md` at
commit `23d0b45` (phase 3a + R0 snapshot). **No files were changed.**

Scope: places where something goes wrong and produces no visible signal — empty lists where
an exception belongs, counters that cannot distinguish "none today" from "broken", swallowed
exceptions, state changes with no log line — plus what cannot be reconstructed afterwards
from the logs, the database and the health footer alone.

Nine findings. The dimension is not clean, but it is also not uniformly bad: the *within-run*
signal is good (the health footer, per-feed notes, the zero-items rule, three adapters that
raise rather than return empty). Almost every finding below is about signal that exists for
one run and then evaporates, or that never reaches a machine.

---

## 1. A run in which every source fails exits 0

**`src/digest/cli.py:93`** — `_run_fetch` returns `0` unconditionally. So does `_run_render`
(`:129`).

**What is wrong.** Exit status carries no information. Five dead sources, a 403 from GitHub,
a DNS outage, an expired mirror — all produce exit code 0, identical to a perfect run.

**Why it matters.** Phase 5 schedules this unattended (PLAN §7: Windows Task Scheduler, WSL
cron, or a GitHub Action). Every one of those decides "did it work?" from the exit code. A
GitHub Action stays green while the digest is empty; cron sends no mail; the seven-consecutive-
days criterion in PLAN §0 would be satisfied by seven consecutive total failures. This is the
single highest-consequence gap in the dimension, and it lands precisely when the project stops
being watched by a human.

**Smallest fix.** Return non-zero when every enabled source failed:

```python
failed = sum(1 for r in result.health if r.consecutive_failures)
return 4 if failed and failed == len(result.health) else 0
```

Total failure only — a partial failure must stay 0, or one dead feed fails the job, which is
the thing CLAUDE.md forbids.

**Bucket: A.**

---

## 2. The mandated per-source run log is invisible unless you pass `-v`

**`src/digest/cli.py:264`** — `level=logging.INFO if args.verbose else logging.WARNING`, and
`_log_source_line` (`:150`) logs at INFO.

**What is wrong.** CLAUDE.md requires "one line per source per run: name, items fetched, new
items, duration, ok/failed". That line is emitted at a level the default configuration
discards.

**Why it matters.** Nobody types `-v` in a crontab. The scheduled run therefore produces no
per-source record anywhere — not on disk, not in the DB, not in the job's captured output.
When `arxiv_cs_ai` starts returning 0 items on a Tuesday, the only artefact that would have
shown it (`fetched=0 new=0 status=ok`) was never written. The footer on stdout does carry
per-source counts, so this is recoverable *if* stdout is captured — but the line CLAUDE.md
specifically asks for, including duration, is not.

**Smallest fix.** Default to INFO for the run log and keep `-v` for DEBUG, or add
`--log-file PATH` for the scheduled path. One line either way.

**Bucket: A.**

---

## 3. A source's failure history is destroyed the moment it recovers

**`src/digest/store.py:450-465`** — `record_source_health` sets `consecutive_failures = 0` on
success. The `sources` table holds only current state; there is no per-run history anywhere.

**What is wrong.** `consecutive_failures` answers "is it failing right now", not "has it been
failing". A source that fails on odd days and succeeds on even days reads as perfectly healthy
every time you look after a success.

**Why it matters.** This is the flapping-feed case, and it is the realistic one: a mirror that
times out under load, a host that rate-limits every second request. PLAN §2.1 introduced the
health record precisely because "a dead feed that fails silently is how these projects rot" —
but a feed that fails *intermittently* rots the same way and leaves no trace at all. You would
have to be watching the terminal at the moment of failure to ever know.

**Smallest fix.** Add `last_failure_at TEXT` and `total_failures INTEGER` to the `sources`
table, both written on failure and neither cleared on success. Two columns, no new table, and
it answers "has this ever been unreliable?" without a runs log.

**Bucket: A.** PLAN §7 phase 4 already anticipates a `runs` table for cost logging; this is a
cheaper subset of that and does not need to wait for it.

---

## 4. A run that inserts nothing leaves no trace that it happened

**`src/digest/cli.py:70-93`** — the only durable writes are item rows and
`record_source_health`.

**What is wrong.** On a run where every item is already stored (the normal case for a second
run in a day, and the normal case when a source goes quiet), the only DB mutation is
`sources.last_success_at`, which is overwritten each time. There is no record that a run
occurred, how many items it fetched, or how many it rejected as duplicates.

**Why it matters.** "Did the 07:00 job run on Tuesday?" is unanswerable after Wednesday's run
overwrites `last_success_at`. So is "how many items did we fetch last Thursday?" — even though
`fetched` and `new` differ by exactly the number that dedupe rejected, which is the metric that
tells you whether a source has stopped producing new material. A run that never happened and a
run that found nothing new are byte-identical in the database.

**Smallest fix.** One row per source per run in a `runs(run_started_at, source, fetched, new,
dupes, duration_ms, status)` table, written from `_run_fetch` where those numbers already exist
(`:79-84`). `first_seen_at` already groups items by run, so this is the missing half.

**Bucket: A.**

---

## 5. Per-feed staleness and failure notes are printed once and never stored

**`src/digest/fetch.py:47`** (`FetchResult.notes`) → **`src/digest/cli.py:90`** — notes reach
`_health_summary` and end at stdout.

**What is wrong.** The most expensive diagnostic in the codebase — per-feed staleness
thresholds, per-feed failure reasons, 304s, `N recent of M` — exists for the duration of one
terminal print.

**Why it matters.** This is the mechanism built in phase 3a specifically because three of six
`ai_blogs` feeds were months stale while returning HTTP 200. That failure mode is *silent by
construction*; the note is the only signal. If you do not read the footer on the morning a feed
crosses its threshold, nothing anywhere records that it was stale. A week later `anthropic_news`
has quietly contributed nothing and the `sources` row still says `ok`, because the source
succeeded — five of its six feeds answered.

**Smallest fix.** Persist notes alongside the health record: a `notes TEXT` column on `sources`
holding the latest run's newline-joined notes. One column, one `UPDATE`, and `digest render`
could then show them too (today it calls `_health_summary` without notes at `:127`, because a
render-only invocation has no `FetchResult`).

**Bucket: A.**

---

## 6. `digest render` silently creates an empty database

**`src/digest/cli.py:103`** — `store.init_db(_db_path(args))`.

**What is wrong.** `init_db` creates the schema when the file is absent. A mistyped `--db`, a
missing `DIGEST_DB_PATH`, or simply running from a different working directory produces a
brand-new empty database and the message `Nothing new. Run 'digest fetch' first.`

**Why it matters.** The failure and the success look the same. `_db_path` (`:49-53`) resolves a
relative default (`./digest.db`) against the current directory, so `cd ~ && digest render` is
enough to trigger it. You conclude the digest is empty; it is actually pointing at a file you
created by looking at it. The same applies to `digest fetch`, where it is less damaging because
you see 456 items arrive.

**Smallest fix.** Add `create: bool = True` to `init_db` and pass `create=False` from
`_run_render`, raising `StoreError` naming the resolved absolute path when the file is missing.
`main` already maps `StoreError` to exit 3 (`:282`).

**Bucket: A.**

---

## 7. The footer cannot distinguish "disabled" from "ran and returned nothing"

**`src/digest/cli.py:90`** with **`src/digest/store.py:468`** — `get_source_health` returns
every row in `sources`, including sources no longer enabled in `sources.yaml`.

**What is wrong.** `_health_summary` prints a line per *persisted* record, and
`counts.get(record.name, 0)` yields 0 for a source that did not run. A source disabled last
month renders as `ok items=0 last_success=<old date>`, visually identical to a source that ran
today and returned nothing.

**Why it matters.** The footer is the primary observability surface, and it currently asserts
something false about sources that were not part of the run. Worse in the direction that
matters: a source you *meant* to disable and a source that has quietly stopped producing look
the same, so neither prompts investigation.

**Smallest fix.** Pass the enabled set into `_health_summary` and mark the others `disabled`,
or filter to the sources actually fetched. The config is already in scope at both call sites.

**Bucket: A.**

---

## 8. arXiv's announce-type filter can drop 100% of input and report success

**`src/digest/adapters/arxiv.py:75`** — `if failures == len(feeds): raise` counts only
*exceptions*. Three categories that parse fine and yield zero kept entries are a successful
fetch.

**What is wrong.** `KEPT_ANNOUNCE_TYPES` (`:28`) filters on a string field. If arXiv renames a
value — `new` → `New`, or drops `arxiv_announce_type` for a differently-named field — every
entry is filtered out. The adapter returns `[]`, the source is `ok`, and the notes say
`cs.AI: 0 new/cross`.

**Why it matters.** This is the highest-volume source (240 items/day) and the filter discards
38% of input by design, so "the filter dropped everything" and "quiet day" are genuinely hard
to tell apart from a single number. Note the *default* is correctly permissive —
`entry.get("arxiv_announce_type", "new")` keeps entries when the field vanishes entirely — so
this only bites on a value rename, not a field rename. That narrows it but does not close it.
`ai_blogs` has the same structural gap at `:126` but its notes carry staleness, which gives the
reader a reason for an empty feed; arXiv's note does not.

**Smallest fix.** Include the pre-filter count in the note: `cs.AI: 0 new/cross of 270
entries`. Zero-of-zero is a quiet day; zero-of-270 is a broken filter, and the two become
distinguishable at a glance.

**Bucket: A.**

---

## 9. `durations` is read with `getattr`, so a rename degrades silently to 0.00s

**`src/digest/cli.py:149`** — `getattr(result, "durations", {}).get(record.name, 0.0)`, with
the parameter typed `result: object` (`:138`).

**What is wrong.** `FetchResult` is a concrete frozen dataclass in the same package. Typing the
parameter `object` and reaching for it via `getattr` with a default defeats both the type
checker and the failure: rename or drop the field and every log line reports
`duration=0.00s` forever, with no error.

**Why it matters.** Low consequence — duration is the least load-bearing column in the log
line — but it is the exact shape of the dimension under review: a default value standing in
for a missing thing, with no signal. Cheap to remove.

**Smallest fix.** Type the parameter `FetchResult` and use `result.durations[record.name]`.

**Bucket: A.**

---

## What cannot be reconstructed afterwards

From the database, the logs and the health footer alone:

**Can be reconstructed**

- every item ever ingested, with its source, title, URL, publication date and raw payload;
- which run first saw each item — `first_seen_at` is one shared timestamp per batch, so it
  groups items by run exactly;
- which items were flagged as near-duplicates at insert time, and of what;
- each source's *current* health: last success, current consecutive-failure count.

**Cannot be reconstructed**

- **how many runs happened, and when** — except the most recent per source (finding 4);
- **whether a source ever failed**, once it has succeeded again (finding 3);
- **whether a feed inside `ai_blogs` was stale on a given day** — the note is printed, never
  stored (finding 5);
- **`fetched` vs `new` for any past run** — so "when did this source stop producing new
  material?" is unanswerable, even though it is the question the health record exists to
  answer (finding 4);
- **whether `render` ever ran**, and what it showed — `digest_date` stamping is deliberately
  deferred to phase 3c, so no item records which digest it appeared in;
- **whether a past run was `--dry-run`** — it writes nothing at all, including no marker;
- **why an item is absent** — filtered by `ai_blogs`'s 30-day cutoff, dropped by arXiv's
  announce-type filter, or below HN's points floor are all recorded as "not present".

The last one is inherent to filtering at ingest and is not worth fixing; the others are
findings 3, 4 and 5.

---

## Buckets

Eight of nine findings are **A** — improvable inside CLAUDE.md's constraints, most of them one
column or one line. That is worth stating plainly: **this dimension found no genuine B.** No
CLAUDE.md constraint is causing the silent-failure problems here. The stdlib-only rule, the
no-network-in-tests rule and the layering rules are all neutral or helpful to observability;
the gaps are omissions, not consequences of the rules.

Two candidates were considered and rejected as B. The "adapters may not log or print" rule
forced the `drain_notes` mechanism into existence, which looks like friction — but the
mechanism works and the real gap is that notes are not persisted (finding 5), which the rule
does not prevent. The "`fetch.py` may not touch the store" rule pushed the run log into
`cli.py`, meaning `fetch_all` called programmatically emits no per-source line — but the only
current non-CLI caller is the snapshot generator, which should be silent anyway.

### One C

**`src/digest/fetch.py:101`** — `except Exception` around every adapter call.

The obvious reading of finding 1 is "stop swallowing exceptions". That would be a regression.
This catch-all is the mechanism behind CLAUDE.md's "never let one failing source abort a run",
and removing or narrowing it reintroduces exactly the failure PLAN §0 lists as a done-criterion
("a source going down degrades one section, never the whole digest"). It has also already been
tuned once, in phase 1, to re-raise non-`Exception` `BaseException` so that a Ctrl-C is not
recorded as source rot (`:152-157`) — the nuance is present and correct.

The right fix for finding 1 is the **exit code**, which reports the aggregate outcome without
changing what the loop tolerates. Catch everything; say so afterwards.
