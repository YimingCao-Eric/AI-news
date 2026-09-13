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
- **Every file read and write passes `encoding="utf-8"` explicitly.** Never a bare
  `open(path, "w")`, `read_text()` or `write_text()`. Windows defaults to the locale
  codepage (cp1252 here), so the first CJK or emoji headline crashes the step with
  `UnicodeEncodeError` — this already nearly shipped in the fetch printer and would hit
  `render` next. CI on Linux defaults to UTF-8 and will never reproduce it, so the tests
  are the only thing standing between this and a 07:00 failure on the laptop.
- Console output goes through `cli._force_utf8_output()` for the same reason.
- All timestamps stored as ISO8601 UTC strings.
- Every module gets a `--dry-run`-able CLI path where it makes sense.
- Log one line per source per run: name, items fetched, new items, duration, ok/failed.
- Type hints everywhere; run `ruff` before declaring done.

## Definition of done for any change
`uv run digest run --dry-run` completes without network errors being fatal, tests pass,
`ruff check` is clean.