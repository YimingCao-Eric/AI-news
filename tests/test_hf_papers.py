"""Hugging Face daily papers adapter.

Fixture recorded with:

    uv run python scripts/record_fixtures.py --source hf_papers
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from digest.adapters.hf_papers import HFPapersAdapter
from digest.errors import SourcePayloadError
from tests.conftest import configured_source, fixture_json, run_adapter, serve

PAPERS = fixture_json("hf_papers.json")


@pytest.fixture
def source():
    return configured_source("hf_papers")


def fetch(payload, source):
    body = json.dumps(payload)
    return run_adapter(HFPapersAdapter(), source, serve(body, content_type="application/json"))


def test_maps_the_recorded_payload(source):
    items = fetch(PAPERS, source)
    assert len(items) == min(len(PAPERS), source.fetch_limit or len(PAPERS))

    first, entry = items[0], PAPERS[0]
    assert first.title == entry["paper"]["title"].strip()
    assert first.source == "hf_papers"
    assert first.topic is None
    assert first.score is None


def test_url_is_the_papers_page(source):
    items = fetch(PAPERS, source)
    for item, entry in zip(items, PAPERS, strict=False):
        assert item.url == f"https://huggingface.co/papers/{entry['paper']['id']}"


def test_author_is_the_submitter_not_the_paper_authors(source):
    """`submittedBy` is who put it on the daily list; paper authors live in raw."""
    entry = {**PAPERS[0], "submittedBy": {"name": "someone", "fullname": "Some One"}}
    assert fetch([entry], source)[0].author == "someone"


def test_published_at_is_timezone_aware(source):
    for item in fetch(PAPERS, source):
        assert item.published_at is not None
        assert item.published_at.tzinfo is not None
        assert item.published_at.utcoffset() == timedelta(0)


def test_published_at_parses_the_z_suffix(source):
    entry = {**PAPERS[0], "publishedAt": "2026-09-09T20:00:00.000Z"}
    assert fetch([entry], source)[0].published_at == datetime(2026, 9, 9, 20, 0, tzinfo=UTC)


def test_raw_keeps_the_whole_entry_including_the_long_summary(source):
    """The abstract belongs in raw, not in a truncated field.

    CLAUDE.md forbids summarising bodies, and PLAN.md section 4 wants the payload intact so
    phase 4 can re-score history. `paper.upvotes` and `githubRepo` ride along for free.
    """
    items = fetch(PAPERS, source)
    assert items[0].raw == PAPERS[0]

    summary = items[0].raw["paper"]["summary"]
    assert len(summary) > 500, "fixture no longer has a long abstract to test against"
    assert summary == PAPERS[0]["paper"]["summary"]
    assert {"upvotes", "id", "title"} <= items[0].raw["paper"].keys()


def test_fetch_limit_is_applied(source):
    assert source.fetch_limit == 50
    trimmed = source.model_copy(update={"fetch_limit": 5})
    assert len(fetch(PAPERS, trimmed)) == 5


def test_zero_papers_raises_rather_than_returning_empty(source):
    """CLAUDE.md zero-items rule.

    daily_papers publishes ~50 every day, so zero means the shape changed. Returning [] would
    be indistinguishable from a quiet day, forever.
    """
    with pytest.raises(SourcePayloadError, match="zero papers"):
        fetch([], source)


def test_non_list_payload_raises(source):
    with pytest.raises(SourcePayloadError, match="expected a JSON list"):
        fetch({"papers": []}, source)


def test_entries_without_an_id_or_title_are_skipped(source):
    payload = [{"paper": {"id": "", "title": "x"}}, {"paper": {"id": "1", "title": ""}}, PAPERS[0]]
    assert len(fetch(payload, source)) == 1


def test_http_error_propagates_to_the_fetch_loop(source):
    """Adapters raise; fetch.py turns it into a health record. Swallowing it here would
    hide the failure from the summary."""
    with pytest.raises(httpx.HTTPStatusError):
        run_adapter(HFPapersAdapter(), source, serve("nope", status=500))
