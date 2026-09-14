"""arXiv adapter: announce-type filtering, title hygiene, round-robin, partial success.

Fixtures recorded with:

    uv run python scripts/record_fixtures.py --source arxiv
"""

from datetime import timedelta

import feedparser
import httpx
import pytest

from digest.adapters.arxiv import (
    KEPT_ANNOUNCE_TYPES,
    ArxivAdapter,
    _round_robin,
    clean_title,
)
from tests.conftest import configured_source, fixture_text, run_adapter, serve_by_url

CS_AI = fixture_text("arxiv_cs_ai.xml")
CS_CL = fixture_text("arxiv_cs_cl.xml")
CS_MA = fixture_text("arxiv_cs_ma.xml")

BODIES = {
    "https://rss.arxiv.org/rss/cs.AI": CS_AI,
    "https://rss.arxiv.org/rss/cs.CL": CS_CL,
    "https://rss.arxiv.org/rss/cs.MA": CS_MA,
}


@pytest.fixture
def source():
    return configured_source("arxiv_cs_ai")


def fetch(source, bodies=None):
    return run_adapter(ArxivAdapter(), source, serve_by_url(bodies or BODIES))


def parsed(xml: str):
    return feedparser.parse(xml).entries


# --------------------------------------------------------------- announce-type filtering


def test_revisions_are_excluded(source):
    """`replace` / `replace-cross` are v2+ of papers that may be months old.

    38% of cs.AI on the recording date. A revision entering the digest as if it were fresh
    is the quiet kind of wrongness that makes you stop trusting the whole thing.
    """
    entries = parsed(CS_AI)
    kept_links = {e.link for e in entries if e.arxiv_announce_type in KEPT_ANNOUNCE_TYPES}
    dropped_links = {e.link for e in entries} - kept_links
    assert dropped_links, "fixture no longer contains any revisions to filter"

    urls = {item.url for item in fetch(source)}
    assert not (urls & dropped_links)


def test_the_announce_filter_is_load_bearing():
    """Proof it earns its place: without it, a third of arXiv would be revisions."""
    entries = parsed(CS_AI)
    revisions = [e for e in entries if e.arxiv_announce_type not in KEPT_ANNOUNCE_TYPES]
    assert len(revisions) / len(entries) > 0.25


def test_new_and_cross_are_both_kept(source):
    entries = parsed(CS_MA)
    expected = {e.link for e in entries if e.arxiv_announce_type in KEPT_ANNOUNCE_TYPES}
    urls = {
        item.url
        for item in fetch(
            source,
            {
                **BODIES,
                "https://rss.arxiv.org/rss/cs.AI": "",
                "https://rss.arxiv.org/rss/cs.CL": "",
            },
        )
    }
    # cs.AI and cs.CL serve empty bodies here, so everything comes from cs.MA.
    assert urls == expected


# ------------------------------------------------------------------------ title hygiene


def test_recorded_titles_carry_no_arxiv_prefix():
    """Documents a measurement, not an assumption.

    The phase 3a brief predicted an `arXiv:XXXX.XXXXX` prefix and embedded newlines in
    titles. Counted across the real fixture: zero of either. That text lives in
    `description`. The strip below stays as a guard for older feed formats, but claiming the
    hazard is present in today's data would be inventing one.
    """
    titles = [e.title for e in parsed(CS_AI)]
    assert len(titles) > 200
    assert not [t for t in titles if t.startswith("arXiv:")]
    assert not [t for t in titles if "\n" in t]
    assert parsed(CS_AI)[0].summary.startswith("arXiv:")  # ...it is in the description


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("arXiv:2609.11977v1 Deep Nets for Cats", "Deep Nets for Cats"),
        ("arXiv:2609.11977 [cs.AI] Deep Nets for Cats", "Deep Nets for Cats"),
        ("Deep  Nets\nfor   Cats", "Deep Nets for Cats"),
        ("  Already clean  ", "Already clean"),
    ],
    ids=["versioned-prefix", "prefix-with-category", "whitespace", "no-op"],
)
def test_clean_title_is_defensive_not_decorative(raw, expected):
    assert clean_title(raw) == expected


def test_a_title_that_merely_mentions_arxiv_is_not_mangled():
    assert clean_title("On the arXiv: a study of preprints") == "On the arXiv: a study of preprints"


# ---------------------------------------------------------------------------- the mapping


def test_maps_url_author_and_aware_dates(source):
    items = fetch(source)
    assert items

    for item in items[:20]:
        assert item.url.startswith("https://arxiv.org/abs/")
        assert item.published_at is not None
        assert item.published_at.tzinfo is not None
        assert item.published_at.utcoffset() == timedelta(0)
        assert item.source == "arxiv_cs_ai"


def test_raw_records_the_category_and_keeps_the_abstract(source):
    items = fetch(source)
    categories = {item.raw["feed_name"] for item in items}
    assert categories == {"cs.AI", "cs.CL", "cs.MA"}
    assert all(item.raw.get("summary") for item in items[:10])


def test_raw_survives_json_serialisation(source):
    """feedparser hands back struct_time and FeedParserDict; store.py json.dumps()es raw."""
    import json

    items = fetch(source)
    assert json.loads(json.dumps(items[0].raw))["feed_name"] in {"cs.AI", "cs.CL", "cs.MA"}


# ----------------------------------------------------------------------- round-robin cap


def test_round_robin_interleaves_categories():
    merged = _round_robin([["a1", "a2", "a3", "a4"], ["b1"], ["c1", "c2"]])
    assert merged[:3] == ["a1", "b1", "c1"]
    assert sorted(merged) == ["a1", "a2", "a3", "a4", "b1", "c1", "c2"]


def test_a_binding_cap_does_not_starve_the_small_category(source):
    """The reason round-robin exists.

    Concatenated, cs.AI's 167 eligible entries would consume any small cap and cs.MA -- the
    multi-agent category the interest profile actually asks for -- would never be stored.
    """
    capped = source.model_copy(update={"fetch_limit": 9})
    categories = {item.raw["feed_name"] for item in fetch(capped)}
    assert categories == {"cs.AI", "cs.CL", "cs.MA"}


def test_the_real_cap_does_not_bind(source):
    """fetch_limit covers everything eligible: an item never stored can never be re-scored."""
    assert source.fetch_limit == 250
    assert len(fetch(source)) < source.fetch_limit


# ------------------------------------------------------------------------ failure shapes


def test_one_dead_category_does_not_lose_the_others(source):
    bodies = {**BODIES, "https://rss.arxiv.org/rss/cs.CL": httpx.Response(503, text="down")}
    adapter = ArxivAdapter()

    items = run_adapter(adapter, source, serve_by_url(bodies))
    categories = {item.raw["feed_name"] for item in items}

    assert categories == {"cs.AI", "cs.MA"}
    notes = adapter.drain_notes()
    assert any("cs.CL" in note and "FAILED" in note for note in notes)
    assert any("cs.AI" in note and "new/cross" in note for note in notes)


def test_every_category_failing_raises(source):
    bodies = dict.fromkeys(BODIES, httpx.Response(503, text="down"))
    with pytest.raises(RuntimeError, match="every category failed"):
        fetch(source, bodies)


def test_unparseable_xml_with_no_entries_raises(source):
    """Zero entries from a broken document is not a quiet day."""
    bodies = dict.fromkeys(BODIES, "<<< not xml at all")
    with pytest.raises(RuntimeError, match="every category failed"):
        fetch(source, bodies)


def test_an_empty_but_valid_feed_is_not_an_error(source):
    """arXiv does not announce every day; empty is a state it can genuinely be in."""
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>cs.AI</title></channel></rss>'
    adapter = ArxivAdapter()
    items = run_adapter(adapter, source, serve_by_url(dict.fromkeys(BODIES, empty)))

    assert items == []
    assert all("0 new/cross" in note for note in adapter.drain_notes())


def test_notes_are_drained_between_runs(source):
    adapter = ArxivAdapter()
    run_adapter(adapter, source, serve_by_url(BODIES))
    assert adapter.drain_notes()
    assert adapter.drain_notes() == []
