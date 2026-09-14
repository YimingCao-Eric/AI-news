"""Hacker News adapter, and the fetch-loop resilience that protects every source.

No network: every request is served by `httpx.MockTransport`, which also lets the tests
assert on the *outgoing* request -- the window and the page size are only observable there.

Async coroutines are driven with `asyncio.run` rather than pytest-asyncio/anyio, to avoid
adding a dev dependency for something the stdlib does in one line.

------------------------------------------------------------------------------------------
Refreshing the fixtures (needs network; not part of the suite). Recorded 2026-09-13:

    uv run python - > tests/fixtures/hn_search_by_date.json <<'PY'
    import json, httpx
    from datetime import UTC, datetime, timedelta
    since = int((datetime.now(tz=UTC) - timedelta(hours=48)).timestamp())
    r = httpx.get("https://hn.algolia.com/api/v1/search_by_date",
                  params={"tags": "story",
                          "numericFilters": f"points>100,created_at_i>{since}",
                          "hitsPerPage": 30},
                  headers={"User-Agent": "AI-news-digest/0.1 (+personal daily digest)"},
                  timeout=20.0)
    r.raise_for_status()
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    PY

    uv run python - > tests/fixtures/hn_ask_hn_null_url.json <<'PY'
    import json, httpx
    r = httpx.get("https://hn.algolia.com/api/v1/search_by_date",
                  params={"tags": "ask_hn", "numericFilters": "points>100", "hitsPerPage": 3},
                  headers={"User-Agent": "AI-news-digest/0.1 (+personal daily digest)"},
                  timeout=20.0)
    r.raise_for_status()
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    PY

The second fixture exists because Ask HN / Show HN text posts are the only hits without a
`url`, and a points-filtered story window structurally contains none of them -- so without
it the fallback would only ever be tested against synthetic data.
------------------------------------------------------------------------------------------
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from digest.adapters.hn import LOOKBACK_HOURS, HNAdapter, build_request
from digest.cli import main
from digest.config import Config, Source, load_config
from digest.fetch import ADAPTERS, fetch_all
from digest.models import Item
from tests.conftest import NetworkAccessInTestError

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]

STORIES = json.loads((FIXTURES / "hn_search_by_date.json").read_text(encoding="utf-8"))
ASK_HN = json.loads((FIXTURES / "hn_ask_hn_null_url.json").read_text(encoding="utf-8"))


@pytest.fixture
def hn_source() -> Source:
    """The real `hn` entry from sources.yaml -- tests the shipped config, not a stand-in."""
    return load_config(REPO_ROOT).sources.by_name("hn")


def _mock_transport(payload: dict, captured: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def fetch_hn(payload: dict, source: Source, captured=None) -> list[Item]:
    """Run the real adapter against a mocked transport."""

    async def run() -> list[Item]:
        async with httpx.AsyncClient(transport=_mock_transport(payload, captured)) as client:
            return await HNAdapter().fetch(client, source)

    return asyncio.run(run())


# ------------------------------------------------------------------- request construction


def test_since_ts_is_48h_back_not_24h(hn_source):
    """The slow-burner guard: a 24h window silently loses stories that peak overnight."""
    now = datetime(2026, 9, 13, 7, 0, 0, tzinfo=UTC)
    _, params = build_request(hn_source, now=now)

    _, _, since = params["numericFilters"].partition("created_at_i>")
    actual = datetime.fromtimestamp(int(since), tz=UTC)

    assert actual == now - timedelta(hours=48)
    assert actual != now - timedelta(hours=24), "regressed to a 24h window"
    assert LOOKBACK_HOURS == 48


def test_points_threshold_survives_substitution(hn_source):
    _, params = build_request(hn_source, now=datetime.now(tz=UTC))
    assert params["numericFilters"].startswith("points>100,")
    assert params["tags"] == "story"


def test_hits_per_page_comes_from_fetch_limit(hn_source):
    _, params = build_request(hn_source, now=datetime.now(tz=UTC))
    assert hn_source.fetch_limit == 30
    assert params["hitsPerPage"] == 30


def test_no_placeholder_survives_substitution(hn_source):
    _, params = build_request(hn_source, now=datetime.now(tz=UTC))
    assert not any("{" in str(value) for value in params.values())


def test_typoed_placeholder_fails_loudly(hn_source):
    """A literal brace reaching Algolia is a coin flip: 400, or unfiltered results."""
    broken = hn_source.model_copy(
        update={
            "url": "https://hn.algolia.com/api/v1/search_by_date"
            "?tags=story&numericFilters=created_at_i>{since-ts}"
        }
    )
    with pytest.raises(ValueError, match=r"unresolved placeholder '\{since-ts\}'"):
        build_request(broken, now=datetime.now(tz=UTC))


def test_special_characters_are_percent_encoded(hn_source):
    """`>` and `,` must be encoded by httpx, not shipped raw and hoped for."""
    captured: list[httpx.Request] = []
    fetch_hn(STORIES, hn_source, captured)

    raw_query = captured[0].url.query.decode()
    assert "%3E" in raw_query and "%2C" in raw_query
    assert ">" not in raw_query

    # ...and still decodes back to what was configured.
    assert parse_qs(raw_query)["numericFilters"][0].startswith("points>100,created_at_i>")


# ------------------------------------------------------------------------------- mapping


def test_maps_stories_to_items(hn_source):
    items = fetch_hn(STORIES, hn_source)
    assert len(items) == len(STORIES["hits"])

    first, hit = items[0], STORIES["hits"][0]
    assert first.title == hit["title"]
    assert first.url == hit["url"]
    assert first.author == hit["author"]
    assert first.source == "hn"
    assert first.topic is None
    assert first.score is None
    assert first.summary is None


def test_raw_is_the_whole_unedited_hit(hn_source):
    """raw is raw: phase 4 re-scores history using fields today's ranker ignores."""
    items = fetch_hn(STORIES, hn_source)
    assert items[0].raw == STORIES["hits"][0]
    assert {"points", "num_comments", "objectID", "_highlightResult"} <= items[0].raw.keys()


def test_published_at_is_timezone_aware_utc(hn_source):
    items = fetch_hn(STORIES, hn_source)
    for item in items:
        assert item.published_at is not None
        assert item.published_at.tzinfo is not None
        assert item.published_at.utcoffset() == timedelta(0)

    assert items[0].published_at == datetime.fromtimestamp(
        STORIES["hits"][0]["created_at_i"], tz=UTC
    )


def test_published_at_falls_back_to_iso_string(hn_source):
    """`created_at_i` is preferred, but a missing one must not produce a naive datetime."""
    hit = {**STORIES["hits"][0], "created_at": "2026-09-12T14:23:11.000Z"}
    del hit["created_at_i"]

    items = fetch_hn({"hits": [hit]}, hn_source)
    assert items[0].published_at == datetime(2026, 9, 12, 14, 23, 11, tzinfo=UTC)


def test_missing_url_falls_back_to_the_hn_thread(hn_source):
    """Ask HN / Show HN text posts have no outbound url; the thread is the artefact.

    Recorded reality: Algolia *omits* the `url` key entirely rather than sending null.
    """
    hits = ASK_HN["hits"]
    assert all("url" not in hit for hit in hits), "fixture no longer covers the missing-url case"

    items = fetch_hn(ASK_HN, hn_source)
    assert len(items) == len(hits)
    for item, hit in zip(items, hits, strict=True):
        assert item.url == f"https://news.ycombinator.com/item?id={hit['objectID']}"


@pytest.mark.parametrize(
    "urlless",
    [{}, {"url": None}, {"url": ""}],
    ids=["key-absent", "explicit-null", "empty-string"],
)
def test_every_urlless_shape_falls_back(hn_source, urlless):
    """`hit.get("url") or fallback` covers all three, so API drift cannot become a crash.

    Today Algolia omits the key. If it ever starts sending `null` or `""` instead -- the
    quiet kind of drift that arrives without a changelog -- the fallback still fires rather
    than producing an Item with an empty url.
    """
    hit = {**ASK_HN["hits"][0], **urlless}
    items = fetch_hn({"hits": [hit]}, hn_source)
    assert items[0].url == f"https://news.ycombinator.com/item?id={hit['objectID']}"


def test_untitled_hits_are_skipped_not_fatal(hn_source):
    good = STORIES["hits"][0]
    payload = {"hits": [{**good, "title": None, "objectID": "1"}, good, {"objectID": "2"}]}
    assert [item.title for item in fetch_hn(payload, hn_source)] == [good["title"]]


def test_empty_response_is_not_an_error(hn_source):
    assert fetch_hn({"hits": []}, hn_source) == []


# ------------------------------------------------------------------- fetch-loop resilience


class _Exploding:
    """An adapter that always raises, standing in for a source having a bad day."""

    name = "gh_trending"

    async def fetch(self, client: httpx.AsyncClient, source: Source) -> list[Item]:
        raise httpx.ConnectError("simulated outage")


def _config_with_enabled(*names: str) -> Config:
    config = load_config(REPO_ROOT)
    config.sources.sources = [
        source.model_copy(update={"enabled": source.name in names})
        for source in config.sources.sources
    ]
    return config


def run_fetch_all(config: Config, monkeypatch, payload: dict = STORIES):
    """Drive fetch_all with every AsyncClient it builds wired to a mock transport."""
    real_client = httpx.AsyncClient

    def client_factory(**kwargs) -> httpx.AsyncClient:
        kwargs["transport"] = _mock_transport(payload)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return asyncio.run(fetch_all(config))


def test_one_failing_source_never_aborts_the_run(monkeypatch):
    """CLAUDE.md's hardest guarantee. Untested resilience works until the first outage."""
    monkeypatch.setitem(ADAPTERS, "gh_trending", _Exploding())
    result = run_fetch_all(_config_with_enabled("hn", "gh_trending"), monkeypatch)
    items, health = result.items, result.health

    assert len(items) == len(STORIES["hits"])
    assert {item.source for item in items} == {"hn"}

    by_name = {record.name: record for record in health}
    assert set(by_name) == {"hn", "gh_trending"}
    assert by_name["hn"].consecutive_failures == 0
    assert by_name["hn"].last_success_at is not None
    assert by_name["gh_trending"].consecutive_failures == 1
    assert by_name["gh_trending"].last_success_at is None


def test_http_error_is_caught_not_raised(monkeypatch):
    """A 500 from the source is a failed source, not a failed run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream is unwell")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handler)}),
    )
    result = asyncio.run(fetch_all(_config_with_enabled("hn")))
    items, health = result.items, result.health

    assert items == []
    assert health[0].consecutive_failures == 1


def test_enabled_source_without_an_adapter_is_a_recorded_failure(monkeypatch):
    """Not a silent skip: that would look identical to a source returning nothing."""
    result = run_fetch_all(_config_with_enabled("arxiv_cs_ai"), monkeypatch)
    items, health = result.items, result.health

    assert items == []
    assert [record.name for record in health] == ["arxiv_cs_ai"]
    assert health[0].consecutive_failures == 1


def test_disabled_sources_get_no_health_record(monkeypatch):
    """sources.yaml is the single source of truth for whether a source runs."""
    health = run_fetch_all(_config_with_enabled("hn"), monkeypatch).health
    assert [record.name for record in health] == ["hn"]


def test_no_enabled_sources_is_not_a_crash(monkeypatch):
    result = run_fetch_all(_config_with_enabled(), monkeypatch)
    assert (result.items, result.health, result.durations) == ([], [], {})


# --------------------------------------------------------------------------- the CLI path


def test_cli_fetch_prints_items_and_health_footer(monkeypatch, capsys, tmp_path):
    # hn only. Phase 3a enabled all five sources, and without this the mocked HN payload
    # would be served to four adapters that correctly reject it -- the test would still
    # pass, but for the wrong reason.
    monkeypatch.setattr("digest.cli.load_config", lambda _: _config_with_enabled("hn"))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(**{**kw, "transport": _mock_transport(STORIES)}),
    )

    assert main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")]) == 0

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(STORIES["hits"]) == sum(line.startswith("[hn] ") for line in lines)
    assert f"fetched {len(STORIES['hits'])}, new {len(STORIES['hits'])}, dupes 0" in out
    assert "hn" in out and "ok" in out


def test_cli_fetch_survives_a_dead_source(monkeypatch, capsys, tmp_path):
    """The footer has to *say* a source failed -- silence is how a dead feed rots unnoticed."""
    monkeypatch.setitem(ADAPTERS, "gh_trending", _Exploding())
    monkeypatch.setattr(
        "digest.cli.load_config", lambda _: _config_with_enabled("hn", "gh_trending")
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(**{**kw, "transport": _mock_transport(STORIES)}),
    )

    assert main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")]) == 0
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "gh_trending" in out
    assert sum(line.startswith("[hn] ") for line in out.splitlines()) == len(STORIES["hits"])


def test_cli_survives_a_title_the_console_codepage_cannot_encode(monkeypatch, capsys, tmp_path):
    """Windows stdout defaults to cp1252; a CJK or emoji headline would otherwise crash."""
    hit = {**STORIES["hits"][0], "title": "中文 model release \U0001f680"}
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(**{**kw, "transport": _mock_transport({"hits": [hit]})}),
    )

    assert main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")]) == 0
    assert "model release" in capsys.readouterr().out


def test_the_network_guard_actually_guards():
    """Meta-test: a guard that silently stops guarding is worse than no guard.

    The blocking itself lives in conftest.py as an autouse fixture so no test can opt out.
    This only proves the fixture is still wired up -- if it ever stops being, the suite would
    quietly go back to hitting Algolia for real, which is exactly how phase 1 started.
    """
    with pytest.raises(NetworkAccessInTestError):
        httpx.get("https://hn.algolia.com/api/v1/search", timeout=1)


def test_the_network_guard_still_allows_loopback():
    """asyncio builds its event-loop self-pipe from a 127.0.0.1 socketpair on Windows."""
    assert asyncio.run(_trivial_coroutine()) == 42


async def _trivial_coroutine() -> int:
    return 42
