# Review: layering and coupling

Interphase review, dimension 2. Reviewed against `CLAUDE.md` and `docs/PLAN.md` at commit
`23d0b45` (phase 3a + R0 snapshot). **No files were changed.**

Scope: does anything violate `fetch → normalise → store → rank → render`; are
`record_source_health`'s success inference and the `drain_notes` contract the right calls;
and what breaks when a sixth adapter arrives.

**The import graph is clean.** No adapter imports `store`. `store` imports only `models`.
`fetch` imports `adapters` and `config` but not `store`. `cli` is the only module that
imports across the whole stack, which is what a composition root is for. There is no cycle,
and no stage reaches downstream. The declared layering is not violated anywhere.

Every finding below is about *shape* rather than violation: vocabulary that means two things,
identity that is conflated with implementation, and policy that lives in the wrong file. Nine
findings plus two constraints I judged to be working.

---

## 1. `SourceHealth` is two different models sharing one name, which forces the store to guess

**`src/digest/store.py:450`**, **`src/digest/models.py:71`**, **`src/digest/fetch.py:113-117`**

**What is wrong.** On the *write* path `SourceHealth` means "what happened in this run":
`fetch.py:117` sets `consecutive_failures=1` and its own comment calls it "a flag, not a
count". On the *read* path (`get_source_health`, `store.py:468`) the same field is a real
accumulated count. Nothing distinguishes the two, so there is no field meaning "did this run
succeed" — and `record_source_health` recovers it by inference: `if health.last_success_at is
not None`.

**Why it matters.** The inference is only correct because of how `fetch.py` happens to build
the object, and the model actively invites the breaking case. The obvious thing for a future
caller to do — a retry wrapper, or `digest run` assembling a complete record — is to carry
the previous `last_success_at` forward so the record is not missing data. That single sensible
act silently converts a failure into a success and resets `consecutive_failures` to 0. There
is no error, no log line, and the health footer then asserts the source is fine. Given this
dimension's neighbour review found that failure history is already destroyed on recovery, the
two compound: a failure recorded as a success is unrecoverable.

**Smallest fix.** Make the outcome explicit in the signature and stop inferring:

```python
def record_source_health(conn, name: str, *, succeeded_at: datetime | None) -> None
```

`None` means failure. One call site (`cli.py:83`), and the store stops depending on a
convention it cannot enforce. Longer term the honest shape is two names — `SourceOutcome` for
the write, `SourceHealth` for the read — but that touches `models.py`, which this phase was
scoped away from.

**Bucket: A.**

---

## 2. The adapter registry conflates "which implementation" with "which source"

**`src/digest/fetch.py:52-61`** — `{adapter.name: adapter for adapter in (...)}`, keyed by
`Adapter.name`, which `base.py:38` documents as "must match the `name` of the source in
sources.yaml this adapter serves".

**What is wrong.** One adapter class can serve exactly one source, because the registry key
*is* the class attribute. There is no way to point a second source at an existing adapter.

**Why it matters.** This is the sixth-adapter question, and the answer is concrete. PLAN §2
Tier 2 lists roughly ten more sources, and most are plain RSS: OpenAI news, DeepMind,
`releases.atom` for eleven watched repos, Reddit as RSS, Lobsters, Apple Newsroom, VideoCardz.
`AIBlogsAdapter` already does everything they need — generic RSS/Atom over a `feeds` list,
per-feed timeouts, staleness. But adding `gh_releases` as a source cannot reuse it. You get
three options, all bad: a subclass whose only content is `name = "gh_releases"`; manual
registry surgery (`ADAPTERS["gh_releases"] = ADAPTERS["ai_blogs"]`), which then shares one
mutable `_notes` between two concurrently-fetched sources and corrupts both (see finding 4);
or cramming unrelated feeds into `ai_blogs` and losing per-source weight, `fetch_limit` and
health.

Note that `Source.kind` already exists for exactly this purpose and is dead (finding 9).

**Smallest fix.** Add an optional `adapter: str | None = None` to `Source`, defaulting to
`name`, and key the registry on adapter class identity instead:

```python
ADAPTERS = {"hn": HNAdapter, "rss": AIBlogsAdapter, ...}          # implementations
adapter_cls = ADAPTERS[source.adapter or source.name]
```

Instantiating per source rather than per class also fixes finding 4 for free.

**Bucket: A.**

---

## 3. The pipeline's composition lives inside an argparse handler

**`src/digest/cli.py:56-93`** — `_run_fetch` opens the database, loops sources, upserts,
counts duplicates, records health, logs and prints.

**What is wrong.** CLAUDE.md names a module per *stage* (`adapters/`, `store.py`, `rank.py`,
`summarise.py`, `render.py`) but none for the *composition*. By default that role fell to
`cli.py`, so the fetch→store pipeline is a private function whose signature is
`(Config, argparse.Namespace)`.

**Why it matters.** `digest run` is the scheduled entry point (PLAN §7 phase 5) and is next
after 3b/3c. It must do fetch→rank→summarise→render, which means it either duplicates this
loop or calls `_run_fetch(config, args)` — a private function that takes an argparse namespace,
forcing `run` to synthesise a fake `Namespace` to call its own sibling. The snapshot generator
already hit the same wall from the other side: `scripts/snapshot_pipeline.py` reimplements
fetch→store in `generate()` because there was no callable composition to reuse, which means
the snapshot protects a composition that is *similar to* but not *identical with* the one the
CLI runs.

**Smallest fix.** Extract the body of `_run_fetch` into `pipeline.py` as
`fetch_and_store(config, db_path, *, dry_run=False) -> RunSummary`, leaving `cli.py` to parse
arguments and print. `run` then calls it, and the snapshot can too.

**Bucket: A.** This is the largest structural finding, but it is additive — nothing has to move
between layers.

---

## 4. `drain_notes` is stateful on a process-lifetime singleton, safe only by an invariant

**`src/digest/adapters/base.py:54`**, **`src/digest/fetch.py:52`** (singletons),
**`src/digest/fetch.py:107`** (drained via a second registry lookup)

**Judgement first, since you asked:** it was the right call at the time and is the wrong shape
to keep. The constraint that produced it — adapters may not log or print — is doing its job
(see C2 below). What is improvable is the mechanism, not the rule.

**What is wrong.** `fetch` is otherwise a pure-ish coroutine; `drain_notes` makes the adapter
a stateful object with a two-call protocol, and `ADAPTERS` holds one instance per class for
the life of the process. The safety argument is a docstring: "each adapter instance serves
exactly one source, and `fetch_all` runs one coroutine per source."

**Why it matters.** That invariant is true today only because the registry key *is*
`adapter.name` (finding 2). The moment someone registers one adapter under two source names —
the natural reaction to finding 2 — two concurrent `fetch_all` coroutines share `self._notes`.
`ai_blogs.py:101` does `self._notes = []` at entry, so the second source to start wipes the
first's notes, and whichever drains first gets a mixture. No exception, no test failure: the
footer just shows the wrong feed diagnostics under the wrong source. Also, `fetch.py:107`
re-looks-up the adapter in the `finally` rather than using the local `adapter`, so a registry
mutated mid-run drains a different object than it fetched.

**Smallest fix.** Have `fetch` return the notes with the items — `AdapterResult(items, notes)`
— making the adapter stateless and the contract single-method. The project already chose a
named result over a growing tuple once (`FetchResult`), so the precedent exists. Cheaper
interim fix: instantiate adapters per source rather than per class, which makes the invariant
structural instead of documented.

**Bucket: A.**

---

## 5. HN's points floor is stated in two config files with nothing reconciling them

**`sources.yaml:25`** (`numericFilters=points>100`) and **`interests.yaml:112`**
(`min_hn_points: 100`). `sources.yaml:16` even says "floor matches
interests.yaml hard_rules.min_hn_points" — a comment is the entire enforcement.

**What is wrong.** The same policy number lives at two layers: one server-side at fetch, one
declared for rank. Nothing reads `min_hn_points` yet (`rank.py` does not exist), so today it
is inert and the duplication is latent.

**Why it matters.** It becomes live in 3b, and it fails in the confusing direction. Lower
`min_hn_points` to 50 expecting more HN items and nothing changes — the fetch URL still filters
at 100 server-side, so those stories never enter the database and no amount of re-ranking can
recover them. The knob that looks like the threshold is not the threshold. Raising it works
fine, which makes the failure asymmetric and easy to misdiagnose.

**Smallest fix.** Give the HN URL a `{min_points}` placeholder resolved from
`interests.hard_rules.min_hn_points`. The placeholder machinery already exists
(`hn.py:21`, `build_request`) and already fails loudly on an unresolved brace — but `fetch`
would need the profile, not just the `Source`, which is finding 6's problem too.

**Bucket: A.**

---

## 6. Ingest-window policy is hardcoded in adapters, unaware of the config that governs it

**`src/digest/adapters/ai_blogs.py:55`** (`MAX_ENTRY_AGE_DAYS = 30`) and
**`src/digest/adapters/hn.py:35`** (`LOOKBACK_HOURS = 48`), against
`interests.yaml` `max_age_hours: 48`.

**What is wrong.** Both constants are selection policy expressed in the fetch layer, and
neither can see `interests.yaml` because `Adapter.fetch(client, source)` receives only its own
`Source`. `hn.py:29-34` explicitly reasons about the relationship to `max_age_hours` and
concludes they must be allowed to diverge — correct — but "allowed to diverge" and "unable to
see each other" are different things.

**Why it matters.** PLAN §8.1 Q1 already names the shape: a filter upstream of a measurement.
Concretely, raise `max_age_hours` to 168 to widen the digest to a week and `ai_blogs` still
discards everything older than 30 days (harmless) while `hn` still only ever *fetched* 48
hours (not harmless — the week's worth was never ingested and cannot be re-ranked). The
intended invariant from `hn.py:33` — ingest window ≥ `max_age_hours` — is stated in a comment
and asserted nowhere, so violating it produces a quietly short digest.

**Smallest fix.** Assert the invariant once at startup in `fetch_all`, where `Config` is in
scope: fail loudly if any adapter's declared lookback is shorter than
`interests.hard_rules.max_age_hours`. That requires adapters to *declare* their window (a
class attribute) rather than to *read* config, so the layering holds.

**Bucket: A.**

---

## 7. `scripts/` depends on a dev-only test library, and `tests/` depends on `scripts/`

**`scripts/snapshot_pipeline.py:40`** (`import pytest`, used for
`pytest.MonkeyPatch.context()`), **`tests/test_snapshot.py:49`**
(`from scripts.snapshot_pipeline import ...`), and `scripts/` has no `__init__.py`.

**What is wrong.** Three couplings at once: a production-adjacent script imports a test
framework; the test tree depends on the scripts tree, which is the reverse of the usual
direction; and the import works only because pytest puts the rootdir on `sys.path`, since
`scripts` is not a package.

**Why it matters.** `uv run python scripts/snapshot_pipeline.py` — the documented way to
regenerate the snapshot — breaks entirely if `pytest` is ever dropped from the dev group,
which is a plausible thing to do to a *script*. And the test import is fragile in a way that
will surface as a confusing `ModuleNotFoundError` rather than a clear one: it depends on
pytest's rootdir insertion, not on anything declared.

**Smallest fix.** Replace `pytest.MonkeyPatch.context()` with
`unittest.mock.patch.object` (stdlib) in `offline()`; add `scripts/__init__.py`. Two small
edits, and `scripts/` stops needing a test dependency.

**Bucket: A.**

---

## 8. `fetch_limit` means two different things depending on the adapter

**`src/digest/adapters/hn.py:88-89`** sets `hitsPerPage` — a *request* parameter. Every other
adapter slices after mapping: **`hf_papers.py:50`**, **`arxiv.py:82`**, **`ai_blogs.py:132`**,
**`gh_trending.py:61`**.

**What is wrong.** One config word names a server-side page size for one source and a
client-side truncation for four.

**Why it matters.** The two have different costs and different failure modes. Raising
`ai_blogs`'s limit stores more of what was already downloaded — free. Raising `hn`'s changes
the outgoing request and can cross a rate limit or change what Algolia returns. And when HN
returns fewer items than `hitsPerPage`, that is invisible: you cannot tell "asked for 30, got
12" from "asked for 12". `sources.yaml:4-7` documents `fetch_limit` as a single concept, so
nothing warns you which kind you are editing.

**Smallest fix.** Document the split where it is configured, and have `hn` also slice
defensively after mapping so the post-condition (`len(items) <= fetch_limit`) is uniform even
if a server ignores the hint.

**Bucket: A.**

---

## 9. `Source.kind` is dead config that looks load-bearing

**`src/digest/config.py:50`** declares it; **`config.py:270`** prints it in
`summarise_config`; nothing else reads it. `base.py:15-19` explicitly warns adapters *not* to
dispatch on it.

**What is wrong.** Every source declares `kind: json_api | html | rss`, and the value does
nothing. It is required (no default), so it looks like a discriminator.

**Why it matters.** Small, but it is exactly the field finding 2 needs. Someone adding the
sixth source will reasonably assume `kind` selects the adapter, discover it does not, and
either add a parallel `adapter:` key — leaving two fields that look like the same thing — or
wire dispatch onto `kind` and contradict `base.py`'s warning. Deciding now is cheaper.

**Smallest fix.** Either give `kind` the dispatch job it appears to have (and rewrite the
`base.py` note, which is really about *bundle detection*, a different question), or drop it to
a comment. Not both meanings, and not neither.

**Bucket: A.**

---

## What breaks when a sixth adapter arrives

Assuming the likely next one — `gh_releases`, eleven `releases.atom` feeds, i.e. generic RSS:

| | |
|---|---|
| **Cannot reuse `AIBlogsAdapter`** | Registry key is `adapter.name` (finding 2). Needs a subclass whose only content is a name. |
| **Sharing one instance corrupts notes** | If you take the shortcut instead, two concurrent sources share `self._notes` (finding 4). Silent: wrong diagnostics under the wrong source. |
| **Two files must be edited in step** | `fetch.py:18-23` imports and `fetch.py:52-61` tuple, plus `adapters/__init__.py`. Forgetting `fetch.py` fails loudly (`LookupError`, `fetch.py:93`) — that part is fine. |
| **`kind` will mislead** | Finding 9. |
| **Staleness thresholds are `ai_blogs`-shaped** | `stale_after_days` is on `Feed` (good, generic), but `DEFAULT_STALENESS_THRESHOLD_DAYS` lives in `ai_blogs.py:52`. A second RSS source importing from `ai_blogs` would be an adapter importing a sibling adapter — the first such edge in the graph. |

Nothing *breaks loudly*. That is the concern: the sixth adapter's cost is paid in duplication
and in one silent failure mode, neither of which a test currently catches.

---

## Constraints I judged to be working

### C1 — the store owning the consecutive-failure increment

**`src/digest/store.py:446-448`.** The obvious layering objection is that computing
`previous + 1` is business logic in a module CLAUDE.md says should "only read/write SQLite".
Moving it to the caller would reintroduce the phase-1 defect exactly: the fetch loop has no
access to the previous value, so the counter could only ever be 0 or 1, and "this source has
failed for three days" would become unrepresentable. Read-modify-write of a stored counter is
persistence, not policy.

**Bucket: C.**

### C2 — adapters may not log or print

**`src/digest/adapters/base.py:10-11`.** The obvious improvement is to let `ai_blogs` log its
own per-feed staleness where it detects it, deleting `drain_notes` entirely. That reintroduces
three things: interleaved output from five concurrently-fetched sources, noisy adapter tests
that currently assert on return values only, and — worst — diagnostics that go straight to a
stream and can therefore never be persisted, which the observability review found is already
the gap. Routing notes through a return value is what makes storing them possible later.

The rule is right. Finding 4 is about the *mechanism*, not the rule.

**Bucket: C.**

---

## On buckets, and the absence of B

All nine findings are **A**. As in the observability review, **no CLAUDE.md constraint is
causing real harm in this dimension** — but since that is now twice, here is the strongest
candidate examined rather than asserted.

**Candidate: "`fetch.py` may not touch the store."** It has four visible consequences: the run
log had to move to `cli.py`; conditional GET for `ai_blogs` was abandoned; `drain_notes` exists
partly because diagnostics cannot be written where they are produced; and `_run_fetch` grew the
per-source persistence loop, which is finding 3.

It is not B, because the constraint is not what makes those bad. Consequences 1 and 4 are fixed
by an orchestrator module (finding 3), not by relaxing the rule. Consequence 2 was independently
judged not worth a schema change. And the rule pays for itself daily: adapter tests need no
database, and the R0 snapshot harness can run `fetch_all` in isolation *because* fetch is
store-free. The risk it protects against — a fetch layer that knows about persistence, making
re-ranking stored history impossible — is exactly what PLAN §4's `raw_json` strategy depends on.

Every finding here is fixable inside the rules. The rules are not the problem; the missing
orchestrator and the overloaded vocabulary are.
