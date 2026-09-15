# AI-news — personal daily digest

One scheduled job: fetch from a fixed source list → dedupe → rank against an interest profile →
summarise the top slice → render one Markdown page I read each morning.

Full design in `docs/PLAN.md`. Interest profile in `docs/interests.md`. Read both before
making design decisions; do not re-derive the architecture.

## Stack — fixed, do not substitute

- Python 3.12, managed with `uv`. Entry point: `digest` CLI.
- httpx (async client), feedparser, selectolax, pydantic v2, jinja2, pyyaml.
- Storage: stdlib `sqlite3`. **No SQLAlchemy, no ORM, no Postgres, no Alembic.**
- CLI: stdlib `argparse`. No Typer/Click.
- Tests: pytest, fixtures only.



## Guiding principle — when in doubt, fail toward the visible error

Where a design choice trades one kind of mistake for another, choose the one you will
*notice*. A visible wrong answer gets corrected; an invisible one becomes the new normal.

This governs at least two subsystems already, and the reasoning is the same in both:

- **URL canonicalisation** strips only unambiguously tracking query params. A false *split*
shows a story twice — you shrug. A false *collapse* drops an item through
`INSERT OR IGNORE`: no row, no log, and the only symptom is missing something you never
knew existed. That asymmetry is why `source` is not stripped.
- **Near-duplicate detection** requires identical numeric tokens on top of the Dice ratio.
It means "at CES 2027" and "at CES" read as different stories — a false negative, so you
see the item twice. The alternative silently merges two different product announcements.

Corollary, for a temptation that will arrive: **do not exempt four-digit years from the
numeric guard.** "CES 2026" and "CES 2027" really are different events. The exemption buys
back a dangerous false-positive class to fix a harmless false-negative one.

### The companion rule: what an adapter does with zero items

A 200 response that parses cleanly and contains nothing is the ambiguous case every adapter
hits eventually. The rule:

> **Raise when zero means "we no longer understand this endpoint". Return** `[]` **when zero is a
> state the source can genuinely be in.**

An empty list is indistinguishable from a quiet day, so returning one for a broken endpoint
is exactly the silent rot the health footer cannot catch. Raising for a genuinely quiet
source is the opposite error and merely noisy. Decide per adapter, and write down which:


| adapter       | zero items                                            | why                                                      |
| ------------- | ----------------------------------------------------- | -------------------------------------------------------- |
| `gh_trending` | **raise**                                             | always ~20 rows; zero means selector rot or a 403 body   |
| `hf_papers`   | **raise**                                             | a fixed daily curated list; zero means the shape changed |
| `arxiv`       | `[]` + warn, but **raise** if feedparser sets `bozo`  | genuinely empty some weekends                            |
| `ai_blogs`    | `[]` per feed; **raise** if every feed yields nothing | one quiet blog is normal, six is not                     |
| `hn`          | `[]`                                                  | a quiet 48h above 100 points is rare but real            |


**Staleness is the third state**, and the one that bites hardest: a feed can return 200 with
well-formed XML whose newest item is four months old. Three of six `ai_blogs` feeds were in
exactly that state when phase 3a measured them. Per-feed staleness thresholds live in
`adapters/ai_blogs.py`; a global threshold either cries wolf on slow feeds or sleeps through
fast ones, and a warning that cries wolf gets ignored, which costs the whole mechanism.

## Hard constraints

- No web framework, no server, no Docker, no task queue, no Celery.
- No new third-party dependency without asking the user first.
- No network access in tests. Every adapter test runs against a recorded fixture in
`tests/fixtures/`. This is *enforced*, not trusted: `tests/conftest.py` has an autouse
session fixture that makes any non-loopback connection raise. Don't weaken it — a phase 0
test went silently online the moment `digest fetch` grew a real implementation under it.
- Never let one failing source abort a run. Catch per-source, record the failure, continue.
- Never sort the Hugging Face models API by `createdAt` (thousands of junk uploads daily) —
use `trendingScore`.
- Set a real User-Agent on every outbound request. Back off on 429.
- Don't fetch or summarise full article bodies. Titles and feed summaries only.



## Layering — keep these separate

fetch → normalise → store → rank → summarise → render → deliver

- `adapters/` only fetch and map to `Item`. No scoring, no DB, no LLM.
- `store.py` only reads/writes SQLite. No business logic.
- `rank.py` is a pure function: (items, profile) -> scored items. No I/O.
- `summarise.py` is the only module allowed to call an LLM.
- `render.py` only turns selected items into text via a jinja2 template.



## Conventions

- **Every file read and write passes** `encoding="utf-8"` **explicitly.** Never a bare
`open(path, "w")`, `read_text()` or `write_text()`. Windows defaults to the locale
codepage (cp1252 here), so the first CJK or emoji headline crashes the step with
`UnicodeEncodeError` — this already nearly shipped in the fetch printer and would hit
`render` next. CI on Linux defaults to UTF-8 and will never reproduce it, so the tests
are the only thing standing between this and a 07:00 failure on the laptop.
- Console output goes through `cli._force_utf8_output()` for the same reason.
- All timestamps stored as ISO8601 UTC strings.
- Every module gets a `--dry-run`-able CLI path where it makes sense.
- Log one line per source per run: name, items fetched, items **inserted**, duration,
  ok/failed. Not "new": that word also names what `digest render` shows, which is items
  never rendered rather than items written this run. The two counts routinely disagree
  and both are correct, so they must not share a word in output.
- Type hints everywhere; run `ruff` before declaring done.
- Where a guard exists to prevent a specific failure, write a test that proves the guard is
**load-bearing** — one that demonstrates the failure the guard prevents, not just that the
code works. `test_the_numeric_guard_is_load_bearing` asserts Dice alone would call
"RTX 5090" and "RTX 5080" duplicates, so deleting the guard produces an explanation rather
than silence. Phase 3b wants the same shape for the per-topic quotas: a test showing the
games section vanishes under 200 AI items when quotas are removed.



## Definition of done for any change

    uv run pytest                          # full suite; the conftest guard keeps it offline
    uv run pytest tests/test_snapshot.py   # the composition is unchanged
    uv run ruff check && uv run ruff format --check

The snapshot test is a contract, not a formality. If it fails, the composition of the
pipeline changed — say what changed and why. NEVER regenerate
`tests/snapshots/pipeline.txt` to make it green; regenerate only after the behaviour
change has been agreed, and say so in the commit message.

(The `digest run --dry-run` clause was removed 2026-09-14: `run` is a stub that exits 0,
so it could not fail. It returns in phase 3c with an assertion about the file it writes.)
