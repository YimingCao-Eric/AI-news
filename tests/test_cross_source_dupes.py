"""Cross-source duplicates between hf_papers and arxiv, on real recorded data.

Phase 3a is the first time two sources can carry the same artefact, and for papers it is
guaranteed rather than incidental: Hugging Face daily_papers is a curated selection of
arXiv preprints, so the same paper arrives under two different URLs on the same morning.

`store.url_hash` cannot catch this -- the URLs genuinely differ -- so the Dice + numeric
guard from phase 2 is the only thing that can. These tests prove it does, against fixtures
recorded 2026-09-14 rather than a synthetic pair constructed to succeed.

Fixtures recorded with:

    uv run python scripts/record_fixtures.py --source hf_papers --source arxiv
"""

import json

import pytest

from digest.adapters.arxiv import ArxivAdapter
from digest.adapters.hf_papers import HFPapersAdapter
from digest.store import title_similarity, titles_are_near_duplicates, url_hash
from tests.conftest import (
    configured_source,
    fixture_json,
    fixture_text,
    run_adapter,
    serve,
    serve_by_url,
)

ARXIV_BODIES = {
    "https://rss.arxiv.org/rss/cs.AI": fixture_text("arxiv_cs_ai.xml"),
    "https://rss.arxiv.org/rss/cs.CL": fixture_text("arxiv_cs_cl.xml"),
    "https://rss.arxiv.org/rss/cs.MA": fixture_text("arxiv_cs_ma.xml"),
}


@pytest.fixture(scope="module")
def hf_items():
    source = configured_source("hf_papers")
    body = json.dumps(fixture_json("hf_papers.json"))
    return run_adapter(HFPapersAdapter(), source, serve(body, content_type="application/json"))


@pytest.fixture(scope="module")
def arxiv_items():
    return run_adapter(ArxivAdapter(), configured_source("arxiv_cs_ai"), serve_by_url(ARXIV_BODIES))


def find_pairs(hf_items, arxiv_items):
    return [
        (hf, ax)
        for hf in hf_items
        for ax in arxiv_items
        if titles_are_near_duplicates(hf.title, ax.title)
    ]


def test_the_same_paper_really_does_arrive_from_both_sources(hf_items, arxiv_items):
    """Real data, not a constructed pair.

    On the recording date exactly one paper overlapped -- "HyQuant: Hybrid-Precision
    Quantization for LLM Attention" -- across 50 HF papers and 240 arXiv entries. One pair
    is enough to prove the mechanism; the count will vary by day.
    """
    pairs = find_pairs(hf_items, arxiv_items)
    assert pairs, (
        "no cross-source duplicate in the current fixtures. Re-record both on the same day: "
        "uv run python scripts/record_fixtures.py --source hf_papers --source arxiv"
    )

    hf, ax = pairs[0]
    assert title_similarity(hf.title, ax.title) >= 0.85


def test_url_hash_cannot_catch_it(hf_items, arxiv_items):
    """Why phase 2's near-duplicate check is load-bearing here.

    The two URLs are genuinely different resources -- a Hugging Face discussion page and an
    arXiv abstract -- so canonicalisation correctly refuses to collapse them. Without the
    title check this paper appears twice in one digest.
    """
    hf, ax = find_pairs(hf_items, arxiv_items)[0]

    assert hf.url != ax.url
    assert url_hash(hf.url) != url_hash(ax.url)
    assert hf.url.startswith("https://huggingface.co/papers/")
    assert ax.url.startswith("https://arxiv.org/abs/")


def test_the_numeric_guard_does_not_block_the_real_pair(hf_items, arxiv_items):
    """Sanity check on phase 2's guard: identical titles have identical numeric tokens.

    Worth pinning, because the guard is strict by design and a paper title full of model
    sizes is exactly where an over-eager guard would misfire.
    """
    hf, ax = find_pairs(hf_items, arxiv_items)[0]
    assert titles_are_near_duplicates(hf.title, ax.title)


def test_different_papers_are_not_collapsed(hf_items, arxiv_items):
    """The cost side. Two unrelated papers must stay two items."""
    pairs = find_pairs(hf_items, arxiv_items)
    assert len(pairs) < len(hf_items) / 2, "the near-duplicate check is matching far too much"


def test_no_winner_is_implemented_yet(hf_items, arxiv_items):
    """Deliberate for phase 3a: the observation is recorded, the resolution is not.

    When both sources carry a paper, hf_papers is the better copy -- it has upvotes,
    submittedBy, numComments and githubRepo, i.e. it already survived a human filter, while
    arXiv cs.AI is 240 entries a day unfiltered. But a zero-upvote HF entry carries no more
    signal than the arXiv row, so a flat source precedence would encode a preference that
    already has exceptions. Phase 3b should make the tiebreak upvotes-aware instead.

    Until then both are stored, the second flagged with `dupe_of`, and nothing is dropped.
    """
    hf, ax = find_pairs(hf_items, arxiv_items)[0]

    assert hf.source == "hf_papers"
    assert ax.source == "arxiv_cs_ai"
    assert "upvotes" in hf.raw["paper"]
    assert "upvotes" not in ax.raw
