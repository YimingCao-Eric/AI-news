"""GitHub Trending scraper: the mapping, and -- more importantly -- that it fails loudly.

Fixture recorded with:

    uv run python scripts/record_fixtures.py --source gh_trending

This is the most fragile adapter in the project: its selectors are Primer CSS utility
classes, which change for cosmetic reasons with no deprecation and no contract. The tests
that matter most here are the ones asserting it *raises* rather than returning an empty
list, because an empty list is indistinguishable from a quiet day and the trending page is
never quiet.
"""

import httpx
import pytest

from digest.adapters.gh_trending import (
    SELECTOR_ROW,
    GhTrendingAdapter,
    GitHubTrendingError,
    parse_trending,
)
from digest.errors import AdapterError, SourceBlockedError
from tests.conftest import configured_source, fixture_text, run_adapter, serve

PAGE = fixture_text("gh_trending.html")


@pytest.fixture
def source():
    return configured_source("gh_trending")


def fetch(source, body=PAGE, status=200):
    return run_adapter(GhTrendingAdapter(), source, serve(body, status, "text/html"))


# ------------------------------------------------------------------------------ mapping


def test_parses_the_recorded_page(source):
    items = fetch(source)
    assert 10 <= len(items) <= 25


def test_urls_and_names_are_well_formed(source):
    for item in fetch(source):
        full_name = item.raw["full_name"]
        assert item.url == f"https://github.com/{full_name}"
        assert full_name.count("/") == 1
        assert not full_name.startswith("/")
        assert item.author == full_name.split("/")[0]


def test_title_carries_the_description(source):
    """`owner/repo` alone gives the keyword ranker nothing to match on.

    These are one-line taglines, not article bodies -- not the thing CLAUDE.md forbids.
    """
    items = fetch(source)
    described = [i for i in items if i.raw["description"]]
    assert described, "fixture has no descriptions to test against"
    for item in described:
        assert item.title.startswith(item.raw["full_name"] + ": ")
        assert item.raw["description"] in item.title


def test_stars_today_is_parsed_to_an_int(source):
    counts = [i.raw["stars_today"] for i in fetch(source)]
    real = [c for c in counts if c is not None]
    assert real, "fixture has no star counts"
    assert all(isinstance(c, int) and c >= 0 for c in real)
    assert max(real) > 10  # thousands-separator commas parsed, not truncated at the comma


def test_language_is_captured_when_present(source):
    languages = {i.raw["language"] for i in fetch(source)}
    assert languages - {None}, "fixture has no languages"


def test_published_at_is_none_not_now(source):
    """The trending page carries no date.

    Faking `now` would make every repo look freshly published to the recency bonus in
    rank.py -- a systematic lie rather than a missing value.
    """
    assert all(item.published_at is None for item in fetch(source))


def test_fetch_limit_is_applied(source):
    capped = source.model_copy(update={"fetch_limit": 3})
    assert len(fetch(capped)) == 3


# ------------------------------------------------------- failing loudly, which is the point


def test_403_raises_with_an_actionable_message(source):
    """GitHub serves a 403 with an HTML body that parses fine and yields zero rows.

    Without the explicit check, a block would look exactly like a quiet day.
    """
    body = "<html><body><h1>Access denied</h1></body></html>"
    with pytest.raises(SourceBlockedError, match="403"):
        fetch(source, body, status=403)


def test_a_layout_change_raises_rather_than_returning_empty(source):
    """The load-bearing test for this adapter.

    An empty list here is indistinguishable from a quiet day, and the trending page is never
    quiet -- it is always ~20 repositories. Returning [] would be silent rot that the health
    footer cannot catch.
    """
    redesigned = "<html><body><div class='NewLayout'><span>rust/rust</span></div></body></html>"
    with pytest.raises(GitHubTrendingError, match=SELECTOR_ROW):
        fetch(source, redesigned)


def test_the_error_names_the_selector_and_the_fix(source):
    with pytest.raises(GitHubTrendingError) as excinfo:
        parse_trending("<html></html>", "gh_trending", "https://github.com/trending")

    message = str(excinfo.value)
    assert SELECTOR_ROW in message
    assert "record_fixtures.py" in message
    assert "gh_trending.py" in message


def test_rows_that_match_but_map_to_nothing_also_raise(source):
    """Half a layout change: the container survived, the repo link moved."""
    body = "<html><body><article class='Box-row'><span>no link here</span></article></body></html>"
    with pytest.raises(GitHubTrendingError, match="mapped none"):
        fetch(source, body)


def test_an_http_error_propagates(source):
    with pytest.raises(httpx.HTTPStatusError):
        fetch(source, "boom", status=500)


def test_every_selector_is_a_module_constant():
    """A layout change should be a one-line fix, not an archaeology session.

    Scans the whole module minus the declared selector block, rather than two named private
    functions. The earlier version called `inspect.getsource` on `parse_trending` and
    `_item_from_row` by name, which made a behaviour-preserving rename or inline fail with
    `AttributeError` -- penalising exactly the refactoring this review exists to enable --
    while a selector inlined into some *third* helper would have passed unnoticed.
    """
    import inspect

    from digest.adapters import gh_trending

    lines = inspect.getsource(gh_trending).splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("# --- Selectors")]
    assert len(starts) == 1, "the selector block marker in gh_trending.py has moved"

    ends = [i for i, line in enumerate(lines) if i > starts[0] and line.startswith("# ----")]
    assert ends, "the selector block is not closed by a `# ----` line"

    outside = "\n".join(lines[: starts[0]] + lines[ends[0] + 1 :])
    for literal in ("Box-row", "itemprop", "d-inline-block"):
        assert literal not in outside, (
            f"selector literal {literal!r} appears outside the declared block in "
            f"gh_trending.py -- a layout change would then need finding, not just editing"
        )


def test_raw_contains_no_clock():
    """Our own timestamp must not live inside `raw`.

    `raw` briefly carried a `scraped_at` set from `datetime.now()`. That duplicated
    `first_seen_at` -- the column that owns "when we saw this" -- and made the row
    irreproducible from a fixed fixture: two runs a second apart over the same HTML produced
    different raw hashes. Found by the R0 snapshot work, fixed rather than excluded from the
    dump, because an exclusion would have made the snapshot tolerant of exactly the defect
    class it exists to catch.
    """
    import json
    from pathlib import Path

    from digest.config import load_config

    source = load_config(Path(__file__).resolve().parents[1]).sources.by_name("gh_trending")
    items = run_adapter(GhTrendingAdapter(), source, serve(PAGE, 200, "text/html"))

    assert set(items[0].raw) == {"full_name", "description", "language", "stars_today"}
    first = json.dumps(items[0].raw, sort_keys=True)
    again = run_adapter(GhTrendingAdapter(), source, serve(PAGE, 200, "text/html"))
    assert json.dumps(again[0].raw, sort_keys=True) == first


def test_a_blocked_host_and_a_broken_layout_are_different_failures():
    """Both are anticipated, but the 07:00 response differs.

    `SourceBlockedError` means check the User-Agent and back off; `GitHubTrendingError`
    means the selectors moved and the fixture needs re-recording. Before the taxonomy both
    were `GitHubTrendingError`, and both were indistinguishable from a crash.
    """
    assert issubclass(SourceBlockedError, AdapterError)
    assert issubclass(GitHubTrendingError, AdapterError)
    assert not issubclass(GitHubTrendingError, SourceBlockedError)
    assert not issubclass(SourceBlockedError, GitHubTrendingError)
