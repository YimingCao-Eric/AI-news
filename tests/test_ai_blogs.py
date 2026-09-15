"""AI-blogs bundle: per-feed isolation, partial success, 304, and the staleness warning.

Fixtures recorded with:

    uv run python scripts/record_fixtures.py --source ai_blogs

The staleness machinery is the reason this adapter is not a ten-line RSS loop. On
2026-09-14 three of six configured feeds were between 49 and 115 days stale while returning
HTTP 200 with well-formed XML -- the mirror is unreliable *per feed*, so one healthy feed
from it says nothing about the others.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from digest.adapters.ai_blogs import (
    MAX_ENTRY_AGE_DAYS,
    AIBlogsAdapter,
    conditional_headers,
    is_stale,
    staleness_threshold,
)
from digest.config import Feed
from tests.conftest import (
    REPO_ROOT,
    configured_interests,
    configured_source,
    fixture_captured_at,
    fixture_text,
    run_adapter,
    serve_by_url,
)

FEED_NAMES = ["openai_news", "deepmind", "anthropic_news", "claude", "anthropic_research", "cursor"]


@pytest.fixture
def source():
    return configured_source("ai_blogs")


@pytest.fixture
def bodies(source):
    return {feed.url: fixture_text(f"ai_blogs_{feed.name}.xml") for feed in source.feeds}


def fetch(source, bodies, now=None):
    """Run the adapter with its clock pinned to when the fixtures were captured.

    Never the live clock. These fixtures are frozen, and the adapter measures both the
    30-day ingest cutoff and every staleness threshold against `now` -- so with a live clock
    this helper's callers had dated expiry: 2026-09-21 for the first, 2026-10-01 for the
    rest. Tests that need a *different* instant pass one explicitly.
    """
    adapter = AIBlogsAdapter(clock=lambda: now or fixture_captured_at())
    items = run_adapter(adapter, source, serve_by_url(bodies))
    return items, adapter.drain_notes()


def rss(title: str, link: str, published: datetime) -> str:
    stamp = published.strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        f"<item><title>{title}</title><link>{link}</link><pubDate>{stamp}</pubDate></item>"
        "</channel></rss>"
    )


# ------------------------------------------------------------------------- the basic map


def test_all_six_feeds_contribute(source, bodies):
    items, notes = fetch(source, bodies)
    assert {item.raw["feed_name"] for item in items} == set(FEED_NAMES)
    assert len(notes) == 6


def test_feed_name_is_recorded_in_raw(source, bodies):
    """Without it, a misbehaving feed is untraceable once six are mixed into one source."""
    items, _ = fetch(source, bodies)
    for item in items:
        assert item.raw["feed_name"] in FEED_NAMES
        assert item.source == "ai_blogs"


def test_dates_come_back_timezone_aware(source, bodies):
    items, _ = fetch(source, bodies)
    dated = [i for i in items if i.published_at is not None]
    assert dated
    for item in dated:
        assert item.published_at.tzinfo is not None


def test_titles_are_whitespace_normalised(source, bodies):
    items, _ = fetch(source, bodies)
    for item in items:
        assert item.title == item.title.strip()
        assert "\n" not in item.title
        assert "  " not in item.title


# ------------------------------------------------------------------- the archive problem


def test_the_back_catalogue_is_not_imported(source, bodies):
    """These feeds carry years of history, unlike arXiv/HF/GitHub which publish today's list.

    Without the cutoff, the first run stored 1824 items across six feeds -- an archive
    import of material `max_age_hours: 48` makes permanently unselectable.
    """
    items, notes = fetch(source, bodies)
    assert len(items) < 300, "the age cutoff is not being applied"

    openai_note = next(n for n in notes if n.startswith("openai_news"))
    assert "recent of" in openai_note


def test_entries_older_than_the_cutoff_are_dropped(source):
    now = fixture_captured_at()
    feed = source.feeds[0]

    def entry(title: str, age_days: int) -> str:
        stamp = (now - timedelta(days=age_days)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        return (
            f"<item><title>{title}</title><link>https://e.example/{title}</link>"
            f"<pubDate>{stamp}</pubDate></item>"
        )

    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        + "".join(entry(f"old{i}", MAX_ENTRY_AGE_DAYS + 10 + i) for i in range(3))
        + entry("fresh", 0)
        + "</channel></rss>"
    )
    single = source.model_copy(update={"feeds": [feed]})
    items, _ = fetch(single, {feed.url: body})
    assert [i.title for i in items] == ["fresh"]


def test_an_entry_with_no_date_is_kept(source):
    """Fail toward the visible error: a duplicate is recoverable, a silent drop is not."""
    feed = source.feeds[0]
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        "<item><title>undated</title><link>https://e.example/x</link></item>"
        "</channel></rss>"
    )
    single = source.model_copy(update={"feeds": [feed]})
    items, _ = fetch(single, {feed.url: body})
    assert [i.title for i in items] == ["undated"]


# ---------------------------------------------------------------------------- staleness


def test_staleness_thresholds_come_from_config(source):
    """Cadence is a property of the feed, so the number lives beside its URL."""
    by_name = {feed.name: feed for feed in source.feeds}
    assert staleness_threshold(by_name["openai_news"]) == 7
    assert staleness_threshold(by_name["anthropic_research"]) == 60
    assert by_name["openai_news"].stale_after_days == 7


def test_a_feed_without_a_threshold_uses_the_default():
    assert staleness_threshold(Feed(name="x", url="https://e.example/f.xml")) == 30


def test_is_stale_uses_the_feeds_own_threshold(source):
    """A global N either cries wolf on slow feeds or sleeps through fast ones."""
    now = datetime(2026, 9, 14, tzinfo=UTC)
    forty_days_ago = now - timedelta(days=40)
    by_name = {feed.name: feed for feed in source.feeds}

    assert is_stale(by_name["openai_news"], forty_days_ago, now)  # threshold 7
    assert not is_stale(by_name["anthropic_research"], forty_days_ago, now)  # threshold 60
    assert is_stale(by_name["cursor"], None, now)


def test_changing_a_threshold_is_a_one_line_config_edit(source):
    """Load-bearing: the whole point of per-feed thresholds is that they are tunable.

    A feed whose cadence turns out to be monthly should stop warning by editing
    sources.yaml, not by editing adapter code.
    """
    feed = source.feeds[0]
    forty_days_ago = datetime(2026, 9, 14, tzinfo=UTC) - timedelta(days=40)
    now = datetime(2026, 9, 14, tzinfo=UTC)

    assert is_stale(feed, forty_days_ago, now)
    relaxed = feed.model_copy(update={"stale_after_days": 90})
    assert not is_stale(relaxed, forty_days_ago, now)


def test_a_stale_feed_is_reported_not_silently_empty(source):
    """The failure this adapter exists for: 200 OK, well-formed, four months dead."""
    feed = source.feeds[0]  # openai_news, threshold 7 days
    old = fixture_captured_at() - timedelta(days=112)
    single = source.model_copy(update={"feeds": [feed]})

    items, notes = fetch(single, {feed.url: rss("ancient", "https://e.example/a", old)})

    assert items == []  # nothing recent enough to store...
    assert "STALE" in notes[0]  # ...but the summary says why
    assert "112d old" in notes[0]
    assert "0 recent of 1" in notes[0]


def test_staleness_is_measured_before_the_age_cutoff(source):
    """The interaction that would otherwise destroy the warning.

    A 112-day-stale feed has zero entries inside the 30-day window. Measuring staleness
    *after* filtering would report a bare "0 items" and lose the signal entirely -- the note
    has to be able to say *why* it is empty.
    """
    feed = source.feeds[0]
    old = fixture_captured_at() - timedelta(days=112)
    single = source.model_copy(update={"feeds": [feed]})

    _, notes = fetch(single, {feed.url: rss("ancient", "https://e.example/a", old)})
    assert "0 entries" not in notes[0], "staleness was measured after filtering"
    assert "STALE" in notes[0]


def test_a_fresh_feed_is_not_marked_stale(source):
    feed = source.feeds[0]
    single = source.model_copy(update={"feeds": [feed]})
    body = rss("today", "https://e.example/a", fixture_captured_at())

    _, notes = fetch(single, {feed.url: body})
    assert "STALE" not in notes[0]


def test_thresholds_are_consistent_with_each_feeds_observed_cadence(source, bodies):
    """Every configured `stale_after_days` must be loose enough for its feed's real rhythm.

    This replaced a test that asserted the recorded feeds were fresh *now*, which had two
    problems: with a live clock it was dated to fail on 2026-09-21 as openai_news crossed its
    7-day threshold, and simply pinning it to `captured_at` would have made it tautological --
    fixtures are fresh at the instant they were captured, so it could never fail. That is the
    same defect as a snapshot test that certifies whatever it is given.

    So it asserts something that can actually be wrong: at the capture instant, each feed's
    newest entry sits inside the threshold configured for it in sources.yaml. Set
    openai_news to `stale_after_days: 1` and this fails, because the recorded feed's own
    cadence contradicts it.

    KNOWN BLIND SPOT, one-sided: it catches thresholds that are too *tight*, never ones that
    are too *loose*. `stale_after_days: 3650` passes happily while silently disabling the
    warning for that feed -- which is the failure the staleness mechanism exists to prevent.
    Catching it needs a median inter-entry gap computed from the recorded entries; the data
    is there (these feeds carry hundreds), but it is more machinery than this guard needs
    today. A small addition later if a threshold is ever widened to quiet a feed.
    """
    captured_at = fixture_captured_at()
    _, notes = fetch(source, bodies, now=captured_at)

    stale = [note for note in notes if "STALE" in note]
    assert not stale, (
        "a configured stale_after_days is tighter than its feed's recorded cadence -- "
        f"either the threshold in sources.yaml is wrong, or the feed was already dying when "
        f"the fixtures were captured: {stale}"
    )


# ----------------------------------------------------------------- isolation and failure


def test_one_dead_feed_does_not_cost_the_other_five(source, bodies):
    """The fix for the phase-1 note on SOURCE_TIMEOUT_SECONDS."""
    broken = {**bodies, source.feeds[2].url: httpx.ConnectTimeout("simulated slow feed")}
    items, notes = fetch(source, broken)

    contributing = {item.raw["feed_name"] for item in items}
    assert len(contributing) == 5
    assert "anthropic_news" not in contributing
    assert any("anthropic_news" in n and "FAILED" in n for n in notes)


def test_a_failed_feed_is_reported_not_silently_absent(source, bodies):
    broken = {**bodies, source.feeds[5].url: httpx.Response(500, text="boom")}
    _, notes = fetch(source, broken)
    assert any("cursor" in n and "FAILED" in n for n in notes)


def test_all_feeds_failing_raises(source, bodies):
    broken = dict.fromkeys(bodies, httpx.ConnectError("network down"))
    with pytest.raises(RuntimeError, match="all 6 feeds failed"):
        fetch(source, broken)


def test_one_quiet_feed_is_not_an_error(source, bodies):
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    items, notes = fetch(source, {**bodies, source.feeds[5].url: empty})
    assert items
    assert any("cursor: 0 entries" in n for n in notes)


# -------------------------------------------------------------------------- conditional


def test_304_is_no_new_items_not_an_error(source, bodies):
    not_modified = {**bodies, source.feeds[1].url: httpx.Response(304)}
    items, notes = fetch(source, not_modified)

    assert "deepmind" not in {item.raw["feed_name"] for item in items}
    assert any("deepmind: not modified (304)" in n for n in notes)
    assert not any("FAILED" in n for n in notes)


def test_conditional_headers_are_empty_until_validators_are_persisted():
    """Deliberate: the `sources` table has one etag column and this source has six feeds.

    Wiring it needs a feed_state table, and the payoff is politeness rather than speed on
    six small files fetched once a day. Deferred to phase 5, if a host ever rate-limits.
    """
    feed = Feed(name="x", url="https://e.example/f.xml")
    assert conditional_headers(feed) == {}
    assert conditional_headers(feed, etag='W/"abc"') == {"If-None-Match": 'W/"abc"'}
    assert conditional_headers(feed, last_modified="Mon, 14 Sep 2026 00:00:00 GMT") == {
        "If-Modified-Since": "Mon, 14 Sep 2026 00:00:00 GMT"
    }


# ------------------------------------------------- notes survive the source failing entirely


def test_notes_survive_the_whole_source_failing(source, bodies, monkeypatch):
    """The most valuable output of a failed run is *which* feeds died and how.

    `_fetch_one` drains in a `finally`, so the per-feed diagnostics come back even though
    the adapter raised. Losing them exactly when something went wrong would be the same
    failure the notes mechanism exists to prevent.
    """
    import asyncio

    from digest.config import Config, SourcesConfig
    from digest.fetch import fetch_all

    dead = dict.fromkeys(bodies, httpx.ConnectError("network down"))
    config = Config(
        sources=SourcesConfig(sources=[source]),
        interests=configured_interests(),
        config_dir=REPO_ROOT,
    )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(serve_by_url(dead))}),
    )
    result = asyncio.run(fetch_all(config))

    assert result.items == []
    assert not result.outcomes[0].succeeded

    notes = result.notes["ai_blogs"]
    assert len(notes) == 6, "per-feed diagnostics were lost when the source failed"
    assert all("FAILED" in note for note in notes)
    for name in FEED_NAMES:
        assert any(note.startswith(name) for note in notes)


def test_every_feed_parsing_clean_and_empty_is_an_error(source, bodies):
    """The all-feeds-empty gap, which existed in both bundle adapters.

    CLAUDE.md's zero-items rule says raise if every feed yields nothing. The check only
    counted *exceptions*, so six well-formed empty documents were a successful fetch of zero
    items -- indistinguishable from six quiet blogs, forever. One quiet blog is normal; six
    at once means the endpoints changed shape.
    """
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    with pytest.raises(RuntimeError, match="zero entries"):
        fetch(source, dict.fromkeys(bodies, empty))


def test_one_empty_feed_among_five_live_ones_is_not_an_error(source, bodies):
    """The other side of it: a single quiet blog must stay unremarkable."""
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    items, notes = fetch(source, {**bodies, source.feeds[5].url: empty})

    assert items
    assert any("cursor: 0 entries" in note for note in notes)


def test_stale_feeds_are_not_mistaken_for_broken_ones(source):
    """Entries that exist but are all older than the cutoff are stale, not a changed shape.

    The guard counts *entries*, not items, precisely so this case still returns cleanly and
    reports STALE rather than raising -- a feed that has gone quiet is a different problem
    from a feed we can no longer parse, and conflating them would make the staleness warning
    unreachable.
    """
    old = fixture_captured_at() - timedelta(days=200)
    bodies = {
        feed.url: rss(f"ancient {feed.name}", f"https://e.example/{feed.name}", old)
        for feed in source.feeds
    }
    items, notes = fetch(source, bodies)

    assert items == []
    assert all("STALE" in note for note in notes)
