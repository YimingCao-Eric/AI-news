# Review: documentation drift

Interphase review, dimension 6. Reviewed against `CLAUDE.md` (103 lines), `docs/PLAN.md`
(455 lines), `README.md` (58 lines) and the code at commit `23d0b45`. **No files were
changed.**

Three sources of truth, and they disagree in eleven places. The pattern is consistent and
worth naming up front: **the documents written *for* a session are current; the documents
written *before* the sessions are stale.** `CLAUDE.md` has been amended in every phase and is
accurate except at two points. `PLAN.md` §4, §5.1, §7 phase 5 and §8.1 were amended as work
landed and are accurate. `PLAN.md` §2, §3 and §7's earlier phases were written on 2026-09-12,
have never been revisited, and now contradict both the code and PLAN's own later sections.
`README.md` has not been touched since phase 0.

Eleven findings.

---

## 1. The stated Definition of Done is satisfied by a no-op

**`CLAUDE.md:102-104`** — "`uv run digest run --dry-run` completes without network errors
being fatal, tests pass, `ruff check` is clean."

**What is wrong.** `run` is still a stub. `cli.py:275-278` routes only `fetch` and `render` to
handlers; everything else falls through to `_not_implemented`, which prints two lines and
returns 0.

```
$ uv run digest run --dry-run
digest run: not implemented
  will: fetch, rank, summarise and render in one pass -- the scheduled entry point
exit=0
```

**Why it matters.** The first clause of the project's own quality gate currently verifies
nothing and cannot fail. A change that breaks every adapter, corrupts the store, or deletes
`fetch_all` still satisfies it exactly as written — and it reads like end-to-end verification,
so a session running it gets false assurance rather than no assurance. That is worse than
having no clause, because it displaces the check someone would otherwise think to do. It will
stay vacuous until phase 4.

**Smallest fix.** Point the clause at the commands that exist: `uv run digest fetch --dry-run`
and `uv run digest render` complete, with a note to switch to `run` when `run` ships.

**Bucket: B** — see the bucket section; this is the one place a constraint is actively causing
harm rather than an omission.

---

## 2. README still describes the project as a phase-0 skeleton

**`README.md:55-56`** — "Phase 0 — skeleton. The CLI parses arguments and loads config; the
pipeline stages are stubs."

**What is wrong.** Three phases out of date. Five adapters, a SQLite store with dedupe, 178
tests and a pipeline snapshot have landed since. Related inaccuracies in the same file:
`:22-24` lists `digest rank` and `digest run` alongside working commands with no indication
that two of the four are stubs; `:27-28` documents `--dry-run` and `--config-dir` but not
`--db` or `-v`, both of which exist and one of which (`--db`) you need to run anything
anywhere but the repo root.

**Why it matters.** This is the first file a stranger opens and the only one a stranger might
open *instead of* the others. Someone told "the pipeline stages are stubs" will not run
`digest fetch` to see what it does, will not find `--db`, and will conclude the fixture
directory and 3 MB of recorded data belong to something unfinished. It also makes the repo
look abandoned at exactly the point where it started working.

**Smallest fix.** Rewrite the Status paragraph to name what is live (`fetch`, `render`, five
sources, store + dedupe) and what is not (`rank`, `run`, LLM, scheduling), and add `--db` and
`-v` to the flags list. Twelve lines.

**Bucket: A.**

---

## 3. A hard constraint has no implementation anywhere

**`CLAUDE.md:71`** — "Set a real User-Agent on every outbound request. **Back off on 429.**"

**What is wrong.** The User-Agent half is implemented (`fetch.py:138`). The 429 half does not
exist: there is no retry, no backoff, no sleep and no status-code handling for 429 anywhere in
`src/`. A 429 becomes an `HTTPStatusError` via `raise_for_status()`, is caught by
`fetch.py:101`, and the source is recorded as failed for the day.

**Why it matters.** This sits under **Hard constraints**, not under a future phase, so a
session reads it as describing existing behaviour and will not implement it. Meanwhile
`gh_trending.py:53-57` raises an error whose text instructs the reader to "back off; do not
retry in a tight loop" — advice that no code follows. PLAN §2 Tier 2 adds Reddit next, which
throttles aggressively; the first 429 there costs a whole day's items from that source with no
retry, and the health footer will report it as a plain failure indistinguishable from a dead
feed.

**Smallest fix.** Move the sentence to a phase-5 item in PLAN and leave a one-line note in
CLAUDE.md saying it is not yet implemented — or implement it. Either is fine; asserting it as
current fact is not.

**Bucket: A.**

---

## 4. The adapter contract names the wrong module as owner of the run log

**`src/digest/adapters/base.py:10-11`** — "print or log -- the caller (`fetch.py`) owns the
one-line-per-source run log".

**What is wrong.** Phase 3a moved that log to `cli.py:136-158`, precisely because the "new
items" column only exists after the store has written and `fetch.py` may not touch the store.
`fetch.py:119-121` documents the move correctly. `base.py` was never updated.

**Why it matters.** `base.py` is the contract document — the file someone writing adapter six
reads first, and the one the duplication review expects to grow a `BundleAdapter`. Sent to
`fetch.py` to find the run log, they will find the drain-notes plumbing and no log line, and
either conclude the docs are unreliable or add a second log line in the place the contract
told them to. Two sources would then each emit half the required line.

**Smallest fix.** Change `fetch.py` to `cli.py` in that sentence, and add the reason —
"because the `new` count only exists after the store has written" — which is the part that
stops it drifting back.

**Bucket: A.**

---

## 5. PLAN's source catalogue states arXiv's volume as 50; it is 270

**`docs/PLAN.md:93`** — "✅ verified — 50 items, same-day (2026-09-12)."

**What is wrong.** Measured on 2026-09-14 during phase 3a: cs.AI 270 entries, cs.CL 115,
cs.MA 16. The figure is off by 5×, and the row carries a ✅ verified mark that makes it read as
checked rather than estimated.

**Why it matters.** `sources.yaml` and PLAN §4 were both corrected; §2 was not, so the two
halves of the same document now disagree. A future session sizing arXiv's `fetch_limit` or a
topic quota from the catalogue table — the obvious place to look — will design for a tenth of
the real volume. The 3a decision to raise `fetch_limit` to 250 looks unjustifiable against a
50-item source, so it is also the kind of discrepancy that invites someone to "correct" a
correct value.

**Smallest fix.** Update the cell and date the measurement, matching the amendment style
already used in §4.

**Bucket: A.**

---

## 6. PLAN's ai_blogs row describes a bundle that no longer exists, including a warning that was resolved

**`docs/PLAN.md:92`** — describes the mirror as covering "Anthropic news/research/engineering,
Meta AI, Mistral, Cohere, xAI, Google AI", with "**check freshness**, the copy I fetched topped
out at 2026-07-14".

**What is wrong.** Phase 3a performed that freshness check, found `anthropic_engineering`
(112d), `meta_ai` (49d) and `mistral` (115d) months stale, dropped all three, and replaced them
with `claude`, `anthropic_research` and `cursor`. `sources.yaml` records the decision, the
measurements and the date. PLAN §2 still reads as though the check is outstanding and the old
feeds are current.

**Why it matters.** The warning was the *instruction* to do the check, and it is still sitting
there having been discharged. A future session reading §2 will either repeat the work or,
worse, "restore" the feeds PLAN lists and quietly re-add three dead sources — the exact
scenario the `store.py` docstring guards against for the schema ("so a future session does not
restore them from the plan"). That guard was applied to §4 and not here.

**Smallest fix.** Replace the freshness warning with its outcome and a pointer to
`sources.yaml`'s dated record.

**Bucket: A.**

---

## 7. PLAN mandates conditional GET on every RSS source; the code deliberately does not

**`docs/PLAN.md:137`** — "Conditional GET (`If-None-Match` / `If-Modified-Since`) on every RSS
source; cache the ETag."

**What is wrong.** Phase 3a implemented 304 *handling* but persists no validators, so every
request goes out unconditional. The reasoning is recorded in
`ai_blogs.conditional_headers`'s docstring: the `sources` table has one `etag` column per
source while `ai_blogs` has six feeds, and the payoff is politeness rather than speed.

**Why it matters.** As written, PLAN §3 is a specification the code silently fails, and the
justification lives only in a function docstring that a session reading the architecture
section will not see. The consequence is wasted work: someone implements it, discovers the
schema problem mid-way, and re-derives a decision that was already made and written down. The
schema columns (`etag`, `last_modified`) still exist and still look like the intended home,
which reinforces the wrong conclusion.

**Smallest fix.** Mark it deferred in §3 with the one-sentence reason and a pointer to the
docstring. Do *not* delete it — the requirement may become live if a host starts rate-limiting,
which is the trigger the docstring names.

**Bucket: A.**

---

## 8. PLAN contradicts itself about what "new" means

**`docs/PLAN.md:308`** — phase 2's done-when: "`digest render` outputs only items first seen
today." **`docs/PLAN.md:~180`** (§4's amendment) — "`digest_date` is what defines 'new':
`new_items()` selects `digest_date IS NULL`, **never a comparison against today's date**",
with two reasons.

**What is wrong.** The same document specifies both behaviours, and §4 explicitly rejects the
one §7 requires.

**Why it matters.** This is the project's most deliberate design decision — the UTC/Vancouver
split and the missed-run catch-up — and it now has a contradiction sitting in the section
someone checks to confirm a phase is complete. A session verifying phase 2 against §7 would
conclude the implementation is wrong and "fix" `store.new_items` into a date comparison,
reintroducing both failures the amendment was written to prevent.

**Smallest fix.** Amend the §7 line to match §4 and cross-reference it. See the C finding
below for why amending rather than rewriting is the right shape.

**Bucket: A.**

---

## 9. PLAN's phase-1 done-when uses a CLI flag that does not exist

**`docs/PLAN.md:303`** — "**Done when:** `digest fetch --source hn` prints 30 titles with
links."

**What is wrong.** There is no `--source` flag. `digest fetch --help` lists `--config-dir`,
`--dry-run`, `--db` and `-v`. Source selection is done by `enabled:` in `sources.yaml`.

**Why it matters.** Small in isolation, but it is a done-when: the line a session runs to
check whether a phase actually shipped. Running it produces an argparse error, which reads as
"phase 1 is broken" rather than "the plan predates the interface". With all five sources now
enabled, there is also no quick way to exercise one adapter against the live endpoint, which is
the capability the flag implied — so the drift hides a genuinely missing affordance.

**Smallest fix.** Amend the line to the `sources.yaml` mechanism, and note `--source` as a
possible future convenience rather than pretending it exists.

**Bucket: A.**

---

## 10. PLAN names a parsing library the stack forbids

**`docs/PLAN.md:90`** — GitHub Trending is "a 30-line BeautifulSoup parse".

**What is wrong.** `CLAUDE.md:11` fixes the stack as `selectolax`, with "do not substitute",
and `gh_trending.py:17` imports `selectolax.parser`. BeautifulSoup is not a dependency.

**Why it matters.** Two project documents name different libraries for the same job, and one of
them is the file that says the stack must not be substituted. A session following PLAN would
add a dependency that CLAUDE.md forbids without asking — and CLAUDE.md's own rule requires
asking first, so the conflict produces exactly the kind of stall the rules exist to prevent.

**Smallest fix.** One word.

**Bucket: A.**

---

## 11. Stale forward references: comments scheduling work for phases that have ended

**`src/digest/adapters/hn.py:33`** — "The right phase 3 move is an assertion that this is >=
max_age_hours, never a merge." **`CLAUDE.md:79-81`** — lists `rank.py`, `summarise.py` and
`render.py` in the layering rules with no indication that none of them exists yet.

**What is wrong.** Phase 3 came and went without the assertion. The comment still reads as a
live instruction. Separately, three of the six modules in the layering section are prospective,
stated in the same present tense as the three that exist.

**Why it matters.** `hn.py:33` is the load-bearing half of a comment that successfully
prevented a merge — the analysis is still correct and worth keeping — but its call to action
now points at a completed phase, so it reads as done when it is not. The invariant it describes
(ingest window ≥ `max_age_hours`) remains unasserted, which the layering review flagged
independently. For CLAUDE.md, a stranger cannot tell from the layering section which files they
will find; minor, but it is the section that defines where code goes.

**Smallest fix.** Re-target the `hn.py` note to phase 3b, where `rank.py` will hold
`max_age_hours`. Mark the three prospective modules in CLAUDE.md with "(phase 3b/4)".

**Bucket: A.**

---

## Buckets

Nine **A**, one **B**, one **C**. This dimension is the first to produce a genuine B, and the
reason is structural: unlike the earlier dimensions, the "constraints" here *are* documents,
so a constraint that has drifted out of usefulness is exactly the failure mode under review.

### The B — CLAUDE.md:102-104, the Definition of Done

**The constraint:** every change must be verified by `uv run digest run --dry-run` completing
without network errors being fatal, plus tests and ruff.

**The harm:** `run` is a stub, so the first clause passes unconditionally and cannot fail. It
supplies false assurance, and false assurance is worse than none because it displaces the check
a session would otherwise improvise. Every phase since 1 has "met" this gate without it
verifying anything.

**What it was protecting against:** shipping a change that passes unit tests while breaking the
pipeline end to end — a real risk, and the reason R0's snapshot exists.

**Does the risk still apply?** Yes, entirely. The intent is right and the instrument is broken.
The fix is not to weaken the rule but to point it at commands that exist today
(`digest fetch --dry-run`, `digest render`) and move it to `run` when `run` ships. Note that
R0 already delivered a better instrument than either — `pytest tests/test_snapshot.py` exercises
the whole composition deterministically — and CLAUDE.md does not mention it.

### The C — PLAN §7's per-phase "Done when" lines

Findings 8 and 9 both sit in §7, and the obvious fix is to rewrite those lines to match what
shipped. That would be a regression.

§7 is a **record of intent at a point in time**, and its value in a project whose recurring bug
class is "two correct decisions interacting" is precisely that it preserves what was believed
*before* the code taught you otherwise. Rewriting phase 2's done-when to say
`digest_date IS NULL` erases the evidence that the plan originally said "first seen today" and
that someone found two concrete reasons to change it — the UTC/Vancouver split and the
missed-run catch-up. That history is why the amendment in §4 is persuasive.

The right shape is the one §4 already uses: leave the original line and append an
**amendment** naming what changed, when, and why. §4's "Amended in phase 2, deliberately — do
not 'restore' these from an earlier draft" is the model; §7 needs the same treatment rather
than a rewrite.

### If you take only two things

**Finding 1**, because it makes every future change's quality gate vacuous, and the fix is one
line. **Finding 4**, because `base.py` is the contract the sixth adapter's author reads first,
and it currently sends them to the wrong file for a rule CLAUDE.md requires them to follow.
