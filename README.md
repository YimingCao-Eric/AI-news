# AI-news

One scheduled job that fetches from a fixed list of AI, hardware and games sources, drops
anything it has seen before, and ranks what is left against a personal interest profile.
The top slice gets summarised by an LLM and rendered into a single dated Markdown page in
`digests/`, which is the whole product — there is no app, no server and no UI. It exists so
that one page each morning replaces half an hour of scrolling Hacker News.

## Running it

Requires [uv](https://docs.astral.sh/uv/); it manages Python 3.12 and everything else.

```bash
uv sync                     # create the venv and install dependencies
uv run digest --help        # list the subcommands
```

The four stages are separate commands so ranking can be iterated on without re-fetching:

```bash
uv run digest fetch         # pull from every enabled source into the store
uv run digest rank          # score stored items against interests.yaml
uv run digest render        # write digests/YYYY-MM-DD.md
uv run digest run           # all of the above, the scheduled entry point
```

Every subcommand takes `--dry-run` (do the work, write nothing) and `--config-dir DIR`
(where to find `sources.yaml` and `interests.yaml`; defaults to the current directory).

## Configuration

| File | What it controls |
|---|---|
| `sources.yaml` | Which feeds to pull, their `weight` in scoring, and the `fetch_limit` on how many items to ingest per run |
| `interests.yaml` | The interest profile: topics with keywords, per-topic `quota` on how many items reach the digest, anti-topics, and hard rules |
| `.env` | API keys and the outbound User-Agent — see `.env.example`. Not needed before phase 4 |

Note the two different caps: `fetch_limit` is per *source* and bounds ingest, `quota` is per
*topic* and bounds selection. Ingest generously — raw payloads are stored so the ranker can
be re-run over history offline.

## Development

```bash
uv run pytest
uv run ruff check
uv run ruff format
```

Tests never touch the network; every adapter is tested against a recorded fixture in
`tests/fixtures/`.

## Status

Phase 0 — skeleton. The CLI parses arguments and loads config; the pipeline stages are
stubs. Design lives in [docs/PLAN.md](docs/PLAN.md), the interest profile it was derived
from in [docs/interests.md](docs/interests.md), and the working rules for changes in
[CLAUDE.md](CLAUDE.md).
