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

from digest.adapters.base import Adapter
from digest.adapters.hn import REQUEST_WINDOW_HOURS, HNAdapter, build_request
from digest.cli import main
from digest.config import Config, Source
from digest.fetch import DEFAULT_USER_AGENT, fetch_all, user_agent
from digest.models import Item
from tests.conftest import NetworkAccessInTestError, load_repo_config

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]

STORIES = json.loads((FIXTURES / "hn_search_by_date.json").read_text(encoding="utf-8"))
ASK_HN = json.loads((FIXTURES / "hn_ask_hn_null_url.json").read_text(encoding="utf-8"))


@pytest.fixture
def hn_source() -> Source:
    """The real `hn` entry from sources.yaml -- tests the shipped config, not a stand-in."""
    return load_repo_config().sources.by_name("hn")


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
    assert REQUEST_WINDOW_HOURS == 48


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


class _Exploding(Adapter):
    """An adapter that always raises, standing in for a source having a bad day."""

    kind = "gh_trending"

    async def fetch(self, client: httpx.AsyncClient, source: Source) -> list[Item]:
        raise httpx.ConnectError("simulated outage")


def _config_with_enabled(*names: str) -> Config:
    config = load_repo_config()
    config.sources.sources = [
        source.model_copy(update={"enabled": source.name in names})
        for source in config.sources.sources
    ]
    return config


def run_fetch_all(config: Config, monkeypatch, payload: dict = STORIES, adapters=None):
    """Drive fetch_all with every AsyncClient it builds wired to a mock transport.

    `adapters` is injected through the public API rather than monkeypatched onto a module
    global: `fetch_all` constructs one instance per source, so there is no registry entry to
    swap, and the mapping merges -- naming one source leaves the rest constructing normally.
    """
    real_client = httpx.AsyncClient

    def client_factory(**kwargs) -> httpx.AsyncClient:
        kwargs["transport"] = _mock_transport(payload)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return asyncio.run(fetch_all(config, adapters=adapters))


def test_one_failing_source_never_aborts_the_run(monkeypatch):
    """CLAUDE.md's hardest guarantee. Untested resilience works until the first outage."""
    result = run_fetch_all(
        _config_with_enabled("hn", "gh_trending"),
        monkeypatch,
        adapters={"gh_trending": _Exploding()},
    )
    items = result.items

    assert len(items) == len(STORIES["hits"])
    assert {item.source for item in items} == {"hn"}

    by_name = {outcome.name: outcome for outcome in result.outcomes}
    assert set(by_name) == {"hn", "gh_trending"}
    assert by_name["hn"].succeeded
    assert by_name["hn"].succeeded_at is not None
    assert not by_name["gh_trending"].succeeded
    assert by_name["gh_trending"].failed_at is not None


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

    assert result.items == []
    assert not result.outcomes[0].succeeded


def test_enabled_source_without_an_adapter_is_a_recorded_failure(monkeypatch):
    """Not a silent skip: that would look identical to a source returning nothing."""
    result = run_fetch_all(_config_with_enabled("arxiv_cs_ai"), monkeypatch)

    assert result.items == []
    assert [outcome.name for outcome in result.outcomes] == ["arxiv_cs_ai"]
    assert not result.outcomes[0].succeeded


def test_disabled_sources_get_no_outcome(monkeypatch):
    """sources.yaml is the single source of truth for whether a source runs."""
    outcomes = run_fetch_all(_config_with_enabled("hn"), monkeypatch).outcomes
    assert [outcome.name for outcome in outcomes] == ["hn"]


def test_no_enabled_sources_is_not_a_crash(monkeypatch):
    result = run_fetch_all(_config_with_enabled(), monkeypatch)
    assert (result.items, result.outcomes, result.durations) == ([], [], {})


# --------------------------------------------------------------------------- the CLI path


def test_cli_fetch_prints_items_and_health_footer(monkeypatch, capsys, tmp_path):
    # hn only. Phase 3a enabled all five sources, and without this the mocked HN payload
    # would be served to four adapters that correctly reject it -- the test would still
    # pass, but for the wrong reason.
    monkeypatch.setattr("digest.cli.load_config", lambda *a, **k: _config_with_enabled("hn"))
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
    assert f"fetched {len(STORIES['hits'])}, inserted {len(STORIES['hits'])}, dupes 0" in out
    assert "hn" in out and "ok" in out


def test_cli_fetch_survives_a_dead_source(monkeypatch, capsys, tmp_path):
    """The footer has to *say* a source failed -- silence is how a dead feed rots unnoticed."""
    monkeypatch.setattr(
        "digest.cli.load_config", lambda *a, **k: _config_with_enabled("hn", "gh_trending")
    )
    monkeypatch.setattr(
        "digest.cli.fetch_all",
        lambda config, **kw: fetch_all(config, adapters={"gh_trending": _Exploding()}),
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


def test_a_real_user_agent_is_sent_on_every_request(monkeypatch):
    """CLAUDE.md hard constraint, previously invisible in 178 tests.

    `MockTransport` does not care what headers arrive, so dropping `headers=` from
    `fetch_all`'s client construction broke nothing in the suite. The first symptom would be
    `gh_trending` returning 403 at 07:00 -- the exact failure its own error message tells you
    to check the User-Agent for -- and Reddit, next in PLAN section 2 Tier 2, throttles
    default agents the same way.
    """
    captured: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def client_factory(**kwargs) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=STORIES)

        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    asyncio.run(fetch_all(_config_with_enabled("hn")))

    assert captured, "no request was made"
    agent = captured[0].headers.get("user-agent", "")
    assert agent == user_agent()
    assert agent
    assert "python-httpx" not in agent.lower(), "httpx's default UA is what gets throttled"
    assert "AI-news" in agent


def test_the_user_agent_is_overridable_from_the_environment(monkeypatch):
    """`.env.example` documents DIGEST_USER_AGENT; nothing asserted it was read."""
    monkeypatch.setenv("DIGEST_USER_AGENT", "custom-agent/9.9 (+contact)")
    assert user_agent() == "custom-agent/9.9 (+contact)"

    monkeypatch.delenv("DIGEST_USER_AGENT", raising=False)
    assert user_agent() == DEFAULT_USER_AGENT
