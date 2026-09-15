# AI-news

One scheduled job that fetches from a fixed list of AI, hardware and games sources, drops
anything it has seen before, and ranks what is left against a personal interest profile.
The top slice gets summarised by an LLM and rendered into a single dated Markdown page in
`digests/`, which is the whole product — there is no app, no server and no UI. It exists so
that one page each morning replaces half an hour of scrolling Hacker News.

## Status — phase 3a

Five sources fetch, normalise, dedupe and store. Ranking, summarising and Markdown rendering
are not built yet.

| Stage | State |
|---|---|
| `fetch` | **Live.** Five sources — Hacker News, GitHub Trending, HF daily papers, an AI-blogs bundle of six feeds, arXiv (cs.AI/cs.CL/cs.MA) — fetched concurrently, one failure never aborting the run |
| store | **Live.** SQLite, URL canonicalisation, `INSERT OR IGNORE` dedupe, near-duplicate flagging |
| `render` | **Partial.** Prints everything not yet shown in a digest, as plain text, and does not consume it. The jinja2 Markdown template is phase 3c |
| `rank` | **Stub.** Prints the loaded config and exits 0 |
| `run` | **Stub.** Same |

An interphase review (R0–R3) ran between 3a and 3b; see `docs/reviews/` and PLAN §7.1.

## Running it

Requires [uv](https://docs.astral.sh/uv/); it manages Python 3.12 and everything else.

```bash
uv sync                     # create the venv and install dependencies
uv run digest --help        # list the subcommands
```

The four stages are separate commands so ranking can be iterated on without re-fetching:

```bash
uv run digest fetch         # pull from every enabled source into the store
uv run digest render        # show what has not been in a digest yet
uv run digest rank          # stub
uv run digest run           # stub; the scheduled entry point once it exists
```

Every subcommand takes the same four flags:

| Flag | Meaning |
|---|---|
| `--config-dir DIR` | Where `sources.yaml` and `interests.yaml` live (default: the current directory) |
| `--db PATH` | SQLite database (default: `$DIGEST_DB_PATH`, else `./digest.db`) |
| `--dry-run` | Do the work, write nothing |
| `-v`, `--verbose` | Add httpx's per-request chatter. The per-source run log is printed **without** this — nobody types `-v` in a crontab |

`$DIGEST_USER_AGENT` overrides the outbound User-Agent. Nothing else reads the environment;
there are no API keys until phase 4.

## Exit codes

This is what a scheduler reads. Any change to it must change `cli.py` and this table together.

| Code | Meaning | What to do |
|---|---|---|
| `0` | Ran. **Including when some sources failed** | Nothing. Read the footer for who died |
| `2` | Bad invocation, or bad config | A human must fix it; retrying will not help. (argparse hardcodes 2 for usage errors, so config shares it) |
| `3` | The database could not be opened, migrated or written | Check the path and the schema version |
| `4` | Every enabled source failed | The environment failed; tomorrow may work. Retry is reasonable |
| `70` | Internal error — the program's own assumptions were violated | A bug. Retrying cannot help; the traceback is in the log |
| `130` | Interrupted (Ctrl-C) | Nothing. Deliberately not `4` |

**Partial failure exits 0 on purpose.** Sources fail routinely, and a code that fires most
mornings gets filtered into a folder nobody opens. "Three of five died" is carried by the
footer instead — which is why the footer has to travel with the digest when phase 5 adds a
delivery channel, not just the items.

## Reading the footer

Every `fetch` and `render` ends with one line per source. The five states are the contract:

```
4 of 5 sources enabled, 189 items this run:
  ai_blogs     ok        items=115  last_success=2026-09-15T02:01:04+00:00
      - cursor: 6 recent of 22, newest 61d old STALE (>45d)
  arxiv_cs_ai  ok        items=74   last_success=2026-09-15T02:01:04+00:00
  gh_trending  disabled  items=—    last_success=2026-09-12T16:31:55+00:00
  hf_papers    FAILED    items=0    last_success=2026-09-13T16:40:02+00:00  consecutive=1 total=3 last_failure=2026-09-15T02:01:06+00:00
  hn           quiet     items=0    last_success=2026-09-15T02:01:05+00:00
```

*(Assembled so all five states appear together; no single run shows all of them. The shapes
are verbatim.)*

| State | Means |
|---|---|
| `ok` | Ran, succeeded, returned items |
| `quiet` | Ran, succeeded, returned **nothing**. Legitimate for `hn` and `arxiv`; for `gh_trending` and `hf_papers` an empty result raises instead, because zero there means the page shape changed |
| `FAILED` | Failed this run. `consecutive=` is the current streak, `total=` the lifetime count — `total=≥N` marks a row whose history predates the counter |
| `disabled` | In `sources.yaml` but not enabled. Counts show `—`, not `0`, because there are none rather than zero |
| `- STALE` | An indented per-feed note under a bundle: HTTP 200, well-formed XML, newest entry months old. Three of six `ai_blogs` feeds were in this state when first measured, and nothing else would have said so |

## Configuration

| File | What it controls |
|---|---|
| `sources.yaml` | Which sources to pull, each with `kind`, `url` **or** `feeds`, `weight`, `fetch_limit`, `enabled` |
| `interests.yaml` | The interest profile: topics with keywords, per-topic `quota`, anti-topics, and hard rules |

`kind` names the adapter **implementation** (`hn`, `gh_trending`, `hf_papers`, `ai_blogs`,
`arxiv`); `name` identifies the source. They coincide today because each implementation
serves one source. An unknown `kind` is rejected when the file loads, not when the source is
fetched.

Note the two different caps: `fetch_limit` is per *source* and bounds ingest, `quota` is per
*topic* and bounds selection. Ingest generously — raw payloads are stored so the ranker can
be re-run over history offline.

Sources can also be cross-referenced: `hn`'s URL carries `{min_points}`, resolved at config
load from `interests.yaml`'s `min_hn_points`, so the points floor is stated once. Any `{...}`
still unresolved at request time is refused rather than sent.

## Development

```bash
uv run pytest                          # full suite, offline
uv run pytest tests/test_snapshot.py   # the pipeline composition is unchanged
uv run ruff check
uv run ruff format --check
```

Those four are the definition of done for any change; see [CLAUDE.md](CLAUDE.md).

Tests never touch the network, and that is **enforced** rather than trusted: an autouse
fixture in `tests/conftest.py` makes any non-loopback connection raise. Every adapter runs
against a recorded fixture in `tests/fixtures/`.

To re-record fixtures — the one thing here that is allowed to use the network:

```bash
uv run python -m scripts.record_fixtures --list
uv run python -m scripts.record_fixtures --source all
```

Use `--source all`. A partial refresh restamps the whole set's `captured_at`, which is the
clock the time-windowed adapters and the snapshot are pinned to.

`tests/snapshots/pipeline.txt` is a characterisation snapshot: fetch → store over every
fixture, 425 sorted rows. It asserts nothing about correctness, only that composition has not
moved, which is what makes it the contract a refactor is measured against. To look at what a
change would do to it without overwriting it:

```bash
uv run python -m scripts.snapshot_pipeline --stdout
```

Regenerate it (no flag — writing is the default) only after the behaviour change has been
agreed, and say in the commit message what moved and why.

## Where the design lives

- [docs/PLAN.md](docs/PLAN.md) — the build plan: source catalogue, data model, phases. §7.1
  indexes the interphase review.
- [docs/interests.md](docs/interests.md) — the interest profile it was derived from.
- [docs/reviews/](docs/reviews/) — the R0–R3 review: six dimension reports, `triage.md` with
  all 54 findings decided, and the deferred list with triggers. **Check `triage.md` before
  proposing a change to anything it touched.**
- [CLAUDE.md](CLAUDE.md) — the working rules for changes.
