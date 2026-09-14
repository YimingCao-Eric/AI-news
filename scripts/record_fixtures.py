"""Record real source responses into tests/fixtures/.

Deliberately outside `tests/`: this is the one thing in the project that *must* touch the
network, and it is not a test. Keeping it here means nobody is tempted to write a recorder as
a test, and it survives four adapters in a way heredocs in docstrings would not.

    uv run python scripts/record_fixtures.py --source all
    uv run python scripts/record_fixtures.py --source arxiv --source gh_trending
    uv run python scripts/record_fixtures.py --list

Each test module names the exact command that produced its fixtures. Re-run when a source
changes shape -- which the adapter tests are there to tell you about.
"""

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SOURCES_YAML = REPO_ROOT / "sources.yaml"

USER_AGENT = "AI-news-digest/0.1 (+https://github.com/YimingCao-Eric/AI-news)"
TIMEOUT = 30.0

ARXIV_CATEGORIES = ("cs.AI", "cs.CL", "cs.MA")


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)


def _write(name: str, text: str) -> None:
    path = FIXTURES / name
    path.parent.mkdir(parents=True, exist_ok=True)
    # CLAUDE.md: always explicit. A CJK paper title would otherwise crash this on Windows.
    path.write_text(text, encoding="utf-8")
    print(f"  wrote {path.relative_to(REPO_ROOT)}  ({len(text.encode('utf-8')) / 1024:.0f} KB)")


def _get(client: httpx.Client, url: str) -> httpx.Response:
    response = client.get(url)
    print(f"  GET {url} -> {response.status_code}")
    response.raise_for_status()
    return response


def _configured(source_name: str) -> dict:
    config = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    for source in config["sources"]:
        if source["name"] == source_name:
            return source
    raise SystemExit(f"no source {source_name!r} in sources.yaml")


# --------------------------------------------------------------------------- per source


def record_hn(client: httpx.Client) -> None:
    """Two fixtures: the story window, and an Ask HN page for the missing-url fallback."""
    from datetime import timedelta

    since = int((datetime.now(tz=UTC) - timedelta(hours=48)).timestamp())
    response = _get(
        client,
        "https://hn.algolia.com/api/v1/search_by_date"
        f"?tags=story&numericFilters=points>100,created_at_i>{since}&hitsPerPage=30",
    )
    _write("hn_search_by_date.json", json.dumps(response.json(), indent=2, ensure_ascii=False))

    # Text posts are the only hits with no `url` key, and a points-filtered story window
    # structurally contains none of them -- hence a second fixture.
    response = _get(
        client,
        "https://hn.algolia.com/api/v1/search_by_date"
        "?tags=ask_hn&numericFilters=points>100&hitsPerPage=3",
    )
    _write("hn_ask_hn_null_url.json", json.dumps(response.json(), indent=2, ensure_ascii=False))


def record_hf_papers(client: httpx.Client) -> None:
    response = _get(client, "https://huggingface.co/api/daily_papers")
    payload = response.json()
    print(f"  {len(payload)} papers")
    _write("hf_papers.json", json.dumps(payload, indent=2, ensure_ascii=False))


def record_arxiv(client: httpx.Client) -> None:
    for category in ARXIV_CATEGORIES:
        response = _get(client, f"https://rss.arxiv.org/rss/{category}")
        _write(f"arxiv_{category.replace('.', '_').lower()}.xml", response.text)


def record_ai_blogs(client: httpx.Client) -> None:
    """Records every feed currently configured, so the fixture set follows sources.yaml."""
    for feed in _configured("ai_blogs")["feeds"]:
        try:
            response = _get(client, feed["url"])
        except httpx.HTTPError as exc:
            print(f"  !! {feed['name']}: {type(exc).__name__}: {exc}")
            continue
        _write(f"ai_blogs_{feed['name']}.xml", response.text)


def record_gh_trending(client: httpx.Client) -> None:
    response = _get(client, "https://github.com/trending?since=daily&spoken_language_code=en")
    _write("gh_trending.html", response.text)


RECORDERS: dict[str, Callable[[httpx.Client], None]] = {
    "hn": record_hn,
    "hf_papers": record_hf_papers,
    "arxiv": record_arxiv,
    "ai_blogs": record_ai_blogs,
    "gh_trending": record_gh_trending,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="record_fixtures",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        action="append",
        choices=[*RECORDERS, "all"],
        metavar="NAME",
        help=f"Source to record; repeatable. One of: {', '.join(RECORDERS)}, all.",
    )
    parser.add_argument("--list", action="store_true", help="List recordable sources and exit.")
    args = parser.parse_args(argv)

    if args.list:
        for name in RECORDERS:
            print(name)
        return 0
    if not args.source:
        parser.error("--source is required (or --list)")

    names = list(RECORDERS) if "all" in args.source else list(dict.fromkeys(args.source))

    failures: list[str] = []
    with _client() as client:
        for name in names:
            print(f"\n[{name}]")
            try:
                RECORDERS[name](client)
            # Broad on purpose: one unreachable source must not stop the other four.
            except Exception as exc:
                print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
                failures.append(name)

    print(f"\nrecorded {len(names) - len(failures)}/{len(names)} source(s)")
    if failures:
        print(f"failed: {', '.join(failures)}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
