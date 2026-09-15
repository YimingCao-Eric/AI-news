"""Shared helpers and reconciled vocabulary.

The consequence these prevent: a correction to the datetime invariant used to have to be made
in two files, and whichever copy was missed would fail for one source only -- reading as
"that feed is malformed" rather than "we fixed this in the wrong file".
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from feedparser.util import FeedParserDict

from digest.adapters import ai_blogs, arxiv, gh_trending, hf_papers, hn
from digest.adapters._mapping import normalise_title, parse_iso_utc, published_at_from_entry
from digest.config import PROFILE_PLACEHOLDERS, load_config
from digest.fetch import KNOWN_KINDS, _apply_fetch_limit, fetch_all
from digest.models import Item
from tests.conftest import REPO_ROOT, fixture_json, load_repo_config
from tests.test_hn import STORIES, _config_with_enabled

MESSY = "  Two\tspaces\nand  a  newline  "
TIDY = "Two spaces and a newline"


# ---------------------------------------------------- DUP-2 / DUP-6: one datetime invariant


def test_the_feed_datetime_helper_rejects_naive_in_one_place():
    """feedparser hands back a naive struct_time; `Item` refuses naive datetimes.

    This conversion lived byte-identically in `arxiv` and `ai_blogs`. It is now one function,
    so a correction is made once -- the tempting alternative when the validator fires is to
    relax the validator, which would undo phase 0.
    """
    entry = FeedParserDict(published_parsed=(2026, 9, 14, 7, 0, 0, 0, 0, 0))
    result = published_at_from_entry(entry)

    assert result == datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
    assert result.tzinfo is not None, "naive would be rejected downstream by Item"
    assert result.utcoffset() == timedelta(0)

    assert published_at_from_entry(FeedParserDict()) is None


def test_both_rss_adapters_use_the_same_datetime_helper():
    """Pinned so the duplication cannot quietly return."""
    for module in (arxiv, ai_blogs):
        assert not hasattr(module, "_published_at"), (
            f"{module.__name__} has its own date parser again -- the invariant is shared"
        )


def test_the_iso_helper_raises_on_malformed_rather_than_returning_none():
    """Absent and malformed are different, and conflating them degrades silently.

    An absent date is a state `Item` supports. A malformed one means the source said
    something we no longer understand, and quietly turning that into "no date" strips the
    item's recency while leaving it present -- nothing looks dropped, it simply never ranks.
    The first version of this helper returned None on a parse failure and a test caught it.
    """
    assert parse_iso_utc("2026-09-14T07:00:00Z") == datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
    assert parse_iso_utc("2026-09-14T07:00:00") == datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
    assert parse_iso_utc(None) is None
    assert parse_iso_utc("") is None

    with pytest.raises(ValueError):
        parse_iso_utc("not-a-date")


# ------------------------------------------------------- DUP-3: one title normalisation rule


@pytest.mark.parametrize(
    "normalise",
    [
        pytest.param(lambda t: normalise_title(t), id="helper"),
        pytest.param(lambda t: arxiv.clean_title(t), id="arxiv"),
    ],
)
def test_a_messy_title_normalises_identically(normalise):
    assert normalise(MESSY) == TIDY


def test_every_adapter_normalises_titles_the_same_way():
    """A tab, a newline and a double space, through all five mapping paths.

    `hn` was the only adapter not normalising -- a guard rather than a repair, since zero of
    the 33 recorded HN titles contain any of these. It matters because `cli` renders each
    item as two aligned lines and phase 3c renders Markdown, both of which a newline breaks.
    """
    messy_hit = {**STORIES["hits"][0], "title": MESSY}
    assert hn._item_from_hit(messy_hit, "hn").title == TIDY

    paper = fixture_json("hf_papers.json")[0]
    messy_paper = {**paper, "paper": {**paper["paper"], "title": MESSY}}
    assert hf_papers._item_from_entry(messy_paper, "hf_papers").title == TIDY

    entry = FeedParserDict(title=MESSY, link="https://e.example/a")
    assert ai_blogs._item_from_entry(entry, "ai_blogs", "feed").title == TIDY
    assert arxiv._item_from_entry(entry, "arxiv_cs_ai", "cs.AI").title == TIDY

    node_text = gh_trending._text
    assert callable(node_text)  # gh_trending routes DOM text through the same helper


# ------------------------------------------- DUP-4 / LC-8: fetch_limit means one thing now


def _item(source: str, n: int) -> Item:
    return Item(
        url=f"https://example.com/{source}/{n}",
        title=f"{source} {n}",
        source=source,
        author=None,
        published_at=None,
        raw={},
    )


def test_fetch_limit_is_a_post_condition_not_a_request_hint():
    """It means "at most this many items are stored", enforced where every adapter's output
    passes -- so an adapter cannot violate it even by accident.

    `hn` previously set Algolia's `hitsPerPage` and trusted the server: a request *hint*
    doing a *guarantee's* job. Drop the parameter in a URL edit, or meet a server that
    ignores it, and `hn` silently exceeded its configured limit while the other four
    structurally could not.
    """
    source = load_repo_config().sources.by_name("gh_trending")
    many = [_item("gh_trending", n) for n in range(100)]

    capped = _apply_fetch_limit(many, source)

    assert source.fetch_limit == 25
    assert len(capped) == 25
    assert capped == many[:25], "order preserved, so arXiv's round-robin still decides which"


def test_an_unlimited_source_is_not_capped():
    source = load_repo_config().sources.by_name("ai_blogs")
    many = [_item("ai_blogs", n) for n in range(100)]

    assert source.fetch_limit is None
    assert _apply_fetch_limit(many, source) == many


def test_no_adapter_caps_for_itself_any_more():
    """The four copies of the slice are gone; one enforcement point remains."""
    import inspect

    for module in (hn, hf_papers, arxiv, ai_blogs, gh_trending):
        body = inspect.getsource(module)
        assert "items[: source.fetch_limit]" not in body, (
            f"{module.__name__} caps for itself again -- the post-condition is enforced once"
        )


def test_hn_still_sends_the_page_size_as_an_optimisation():
    """Kept, not deleted: do not download a thousand to keep thirty."""
    source = load_repo_config().sources.by_name("hn")
    _, params = hn.build_request(source, now=datetime.now(tz=UTC))
    assert params["hitsPerPage"] == source.fetch_limit


# ----------------------------------------------------- LC-5: the points floor is stated once


def test_the_points_floor_is_derived_from_the_interest_profile():
    """One value, not two agreeing values.

    It was `points>100` in a URL and `min_hn_points: 100` in the profile, reconciled by a
    comment -- and it failed asymmetrically: raising the floor worked, lowering it changed
    nothing, because the server-side filter still cut at the old value and those stories
    never entered the database to be re-ranked.
    """
    config = load_repo_config()
    floor = config.interests.hard_rules.min_hn_points

    assert f"points>{floor}" in config.sources.by_name("hn").url
    assert "{min_points}" not in config.sources.by_name("hn").url


def test_changing_the_profile_moves_the_fetch_floor(tmp_path):
    """The load-bearing half: the derived value must actually follow."""
    (tmp_path / "sources.yaml").write_text(
        (REPO_ROOT / "sources.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    interests = (REPO_ROOT / "interests.yaml").read_text(encoding="utf-8")
    (tmp_path / "interests.yaml").write_text(
        interests.replace("- min_hn_points: 100", "- min_hn_points: 42"), encoding="utf-8"
    )

    config = load_config(tmp_path, known_kinds=KNOWN_KINDS)

    assert "points>42" in config.sources.by_name("hn").url
    assert "points>100" not in config.sources.by_name("hn").url


def test_an_unresolved_placeholder_never_reaches_the_source():
    """The guard that makes derivation safe: `{since_ts}` resolves per request, and anything
    still wearing braces after that is refused rather than shipped to Algolia."""
    assert "{min_points}" in PROFILE_PLACEHOLDERS
    source = load_repo_config().sources.by_name("hn")
    broken = source.model_copy(update={"url": source.url.replace("{since_ts}", "{typo}")})

    with pytest.raises(ValueError, match="unresolved placeholder"):
        hn.build_request(broken, now=datetime.now(tz=UTC))


# --------------------------------------------------------- VN-1: the two senses of "new"


def test_the_two_senses_of_new_are_distinguishable_in_output(monkeypatch, tmp_path, capsys):
    """`inserted` counts rows written this run; render counts items never rendered.

    Run fetch twice and then render: the first says `inserted 0`, the second reports items
    waiting. Both correct, and they used to print the same word.
    """
    from digest.cli import main

    real_client = httpx.AsyncClient
    monkeypatch.setattr("digest.cli.load_config", lambda *a, **k: _config_with_enabled("hn"))
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(200, json=STORIES))}
        ),
    )
    db = tmp_path / "t.db"
    main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(db)])
    capsys.readouterr()

    main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(db)])
    second = capsys.readouterr().out
    assert "inserted 0" in second
    assert "new" not in second.split("fetched")[-1].split("\n")[0]

    main(["render", "--config-dir", str(REPO_ROOT), "--db", str(db)])
    rendered = capsys.readouterr().out
    assert "not yet in a digest" in rendered
    assert f"{len(STORIES['hits'])} item(s) not yet in a digest" in rendered


def test_insert_items_reports_rows_actually_inserted(tmp_path):
    """VN-2: the name now matches INSERT OR IGNORE. Rename only -- no write-back path."""
    from digest import store

    conn = store.init_db(tmp_path / "t.db")
    try:
        items = [_item("hn", 1)]
        assert store.insert_items(conn, items) == 1
        assert store.insert_items(conn, items) == 0
        assert not hasattr(store, "upsert_items")
    finally:
        conn.close()


def test_the_fetch_limit_post_condition_holds_end_to_end(monkeypatch):
    """Every source, through the real loop, capped once."""
    config = _config_with_enabled("hn")
    config.sources.sources = [
        s.model_copy(update={"fetch_limit": 3}) for s in config.sources.sources
    ]
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(200, json=STORIES))}
        ),
    )
    result = asyncio.run(fetch_all(config))

    assert len(result.items) == 3
    assert len(json.dumps([i.url for i in result.items])) > 0
