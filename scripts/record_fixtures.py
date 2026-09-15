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

from digest.adapters.hn import build_request
from digest.config import load_config
from digest.fetch import KNOWN_KINDS

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SOURCES_YAML = REPO_ROOT / "sources.yaml"

USER_AGENT = "AI-news-digest/0.1 (+https://github.com/YimingCao-Eric/AI-news)"
TIMEOUT = 30.0

ARXIV_CATEGORIES = ("cs.AI", "cs.CL", "cs.MA")

#: Written on every run. The pipeline snapshot pins its clock to `captured_at` from here.
#:
#: This is not decoration. `ai_blogs` filters entries against `now - MAX_ENTRY_AGE_DAYS`,
#: so a snapshot generated with a hardcoded date would produce zero rows once the fixtures
#: aged past the cutoff -- a snapshot of nothing, produced by a test that passes. Deriving
#: the pinned clock from the fixture set means re-recording updates it as a side effect,
#: rather than as something someone has to remember.
MANIFEST_FILENAME = "manifest.json"


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
    """Two fixtures: the story window, and an Ask HN page for the missing-url fallback.

    The story request is built by the adapter's own `build_request` against the loaded
    config, not hand-written here. It used to be a literal URL duplicating sources.yaml --
    the points floor, the 48h window and the page size all restated -- so changing any of
    them in config left the recorder capturing a window production no longer uses, and the
    snapshot would have been built from fixtures that did not match the pipeline.

    Routing through `build_request` also puts the recorder behind the guard that refuses to
    ship an unresolved `{...}`: `{min_points}` is resolved at config load and `{since_ts}` per
    request, so a recorder reading raw YAML would have sent Algolia a literal brace and the
    next refresh would have captured a wrong or empty fixture, weeks before anyone noticed.
    """
    source = load_config(REPO_ROOT, known_kinds=KNOWN_KINDS).sources.by_name("hn")
    base_url, params = build_request(source, now=datetime.now(tz=UTC))
    request = httpx.Request("GET", base_url, params=params)
    response = _get(client, str(request.url))
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


def write_manifest(sources: list[str], captured_at: datetime) -> None:
    """Record when this fixture set was captured, and from which sources."""
    manifest = {
        "captured_at": captured_at.astimezone(UTC).isoformat(),
        "sources": sorted(sources),
        "note": (
            "captured_at pins the clock for scripts/snapshot_pipeline.py. Time-windowed "
            "adapters (ai_blogs MAX_ENTRY_AGE_DAYS) produce a different row set as "
            "fixtures age, so the snapshot is only reproducible against this instant."
        ),
    }
    _write(MANIFEST_FILENAME, json.dumps(manifest, indent=2) + "\n")


def newest_fixture_mtime() -> datetime:
    """Capture instant for a fixture set recorded before manifests existed."""
    files = [p for p in FIXTURES.glob("*") if p.name != MANIFEST_FILENAME]
    if not files:
        raise SystemExit("no fixtures to stamp")
    return datetime.fromtimestamp(max(p.stat().st_mtime for p in files), tz=UTC)


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
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help=(
            "Write manifest.json for the fixtures already on disk, without fetching. "
            "For a set recorded before manifests existed; captured_at comes from the "
            "newest fixture mtime."
        ),
    )
    args = parser.parse_args(argv)

    if args.list:
        for name in RECORDERS:
            print(name)
        return 0
    if args.manifest_only:
        captured_at = newest_fixture_mtime()
        write_manifest(list(RECORDERS), captured_at)
        print(f"stamped existing fixtures: captured_at={captured_at.isoformat()}")
        return 0
    if not args.source:
        parser.error("--source is required (or --list)")

    names = list(RECORDERS) if "all" in args.source else list(dict.fromkeys(args.source))

    failures: list[str] = []
    started_at = datetime.now(tz=UTC)
    with _client() as client:
        for name in names:
            print(f"\n[{name}]")
            try:
                RECORDERS[name](client)
            # Broad on purpose: one unreachable source must not stop the other four.
            except Exception as exc:
                print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
                failures.append(name)

    # Written even on partial failure: the manifest describes the fixture set on disk, and
    # a half-refreshed set still needs its clock pinned to when it was captured.
    write_manifest(names, started_at)

    print(f"\nrecorded {len(names) - len(failures)}/{len(names)} source(s)")
    if failures:
        print(f"failed: {', '.join(failures)}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
