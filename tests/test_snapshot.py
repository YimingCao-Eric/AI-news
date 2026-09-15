"""The pipeline snapshot: a contract for every refactor that follows.

WHAT THIS PROTECTS
------------------
The **composition** of the pipeline, which the other unit tests structurally cannot cover
because they test the parts. Each adapter has its own tests; `store.py` has its own tests.
Nothing else asserts what happens when they are wired together and run end to end:

* cross-source deduplication -- the same paper arriving from `hf_papers` and `arxiv_cs_ai`
  under different URLs, where `url_hash` cannot help and the near-duplicate check must;
* the **direction** of every `dupe_of`, which currently depends on the order of entries in
  `sources.yaml` -- incidental ordering in a file that looks like formatting;
* URL canonicalisation collapsing (or refusing to collapse) rows across five sources at once;
* per-source row counts, so a filter that silently starts dropping everything is visible;
* `published_at` parsing across four different date formats reaching one store;
* which items survive `ai_blogs`'s age cutoff.

A refactor that changes any of those changes this file. That is the point: the snapshot is
the net under R1's layering work and R3's fixes, which rearrange how the pieces compose.

UPDATING IT IS AN EXPLICIT DECISION, NEVER A CONVENIENCE
--------------------------------------------------------
When this test fails, the first question is "what did I change, and did I mean to change
it?" -- not "how do I make it green". Regenerate only when you can state what moved and why:

    uv run python scripts/snapshot_pipeline.py

and put that explanation in the commit message, so the diff of `pipeline.txt` has a reason
attached. Regenerating to silence a red test discards the only record of what the pipeline
used to do.

Re-recording fixtures legitimately changes it (different papers that day), and that is fine
*as its own commit*, separate from a behaviour change, so the two diffs never mix.

WHAT IT DELIBERATELY DOES NOT COVER
-----------------------------------
The run's own clock (`first_seen_at`, `last_success_at`, durations) and the temp database
path. Those are injected environment, not source data. The exclusion list is kept short on
purpose: anything excluded here is something the snapshot stops testing forever, and every
future nondeterminism will arrive with a plausible case for being added to it. The
`scraped_at` bug found while building this is the worked example -- it was fixed at source
rather than excluded from the dump.
"""

import difflib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scripts.snapshot_pipeline import SNAPSHOT_PATH, SnapshotError, generate, snapshot_now

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def dump() -> str:
    """One pipeline generation, shared by every test that only reads the result.

    `generate()` re-parses 3 MB of fixtures -- 572 KB of arXiv XML and 717 KB of OpenAI RSS
    among them -- and was being called six times across this module, making the snapshot
    suite 6.8s of a 12s run. R3 runs this file constantly, so the cost compounds. Tests that
    need a *second* generation (determinism) or a *different* one (a broken harness) still
    call it themselves; sharing would defeat their subject.
    """
    return generate()


def test_pipeline_matches_the_committed_snapshot(dump):
    """The contract. A readable diff, or nothing."""
    assert SNAPSHOT_PATH.exists(), (
        f"{SNAPSHOT_PATH} is missing. Generate it with "
        f"`uv run python scripts/snapshot_pipeline.py`."
    )

    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    actual = dump

    if actual != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual.splitlines(),
                fromfile="tests/snapshots/pipeline.txt (committed)",
                tofile="generated now",
                lineterm="",
                n=2,
            )
        )
        pytest.fail(
            "The pipeline's end-to-end output changed.\n\n"
            "Decide whether you meant this before regenerating. If you did, run\n"
            "  uv run python scripts/snapshot_pipeline.py\n"
            "and say in the commit message what changed and why.\n\n" + diff,
            pytrace=False,
        )


def test_generation_is_deterministic(dump):
    """Two independent generations in one process must be byte-identical.

    Catches the class of bug that produced this file: a clock or an unordered iteration
    leaking into output that is supposed to be reproducible. `dump` is a separate, earlier
    generation, so this is still two real runs -- one call rather than two, which halves the
    most expensive test in the suite.
    """
    assert generate() == dump


def test_the_clock_follows_the_manifest_not_a_constant(tmp_path, monkeypatch):
    """Load-bearing: replace `snapshot_now`'s body with a constant and this must fail.

    The previous version asserted only that the manifest existed and that `snapshot_now()`
    returned an aware datetime -- both of which a hardcoded
    `return datetime(2026, 9, 14, tzinfo=UTC)` satisfies. It therefore certified the exact
    design R0 rejected, which is worse than no test: someone simplifying the indirection away
    would have got a green suite.

    What the indirection is for: `ai_blogs` filters against `now - INGEST_MAX_AGE_DAYS`, so a
    constant frozen at one date pushes every entry outside the cutoff the moment fixtures are
    re-recorded -- yielding a snapshot of nothing, produced by a test that still passes.
    Reading `captured_at` means re-recording moves the clock as a side effect.
    """
    from scripts import snapshot_pipeline

    moved = tmp_path / "manifest.json"
    moved.write_text(
        json.dumps({"captured_at": "2031-07-04T12:34:56+00:00", "sources": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(snapshot_pipeline, "MANIFEST_PATH", moved)

    assert snapshot_now() == datetime(2031, 7, 4, 12, 34, 56, tzinfo=UTC)


def test_a_missing_manifest_fails_loudly(tmp_path, monkeypatch):
    """Without it there is no pinned clock, so a silent fallback would be a live one."""
    from scripts import snapshot_pipeline

    monkeypatch.setattr(snapshot_pipeline, "MANIFEST_PATH", tmp_path / "absent.json")
    with pytest.raises(SnapshotError, match="is missing"):
        snapshot_now()


def test_the_snapshot_exercises_the_age_cutoff(dump):
    """Load-bearing: the snapshot must keep testing the filter, not just pass.

    `generate()` raises if `ai_blogs` keeps none of its entries or all of them -- either
    would mean the time window has stopped being exercised, and the bug that motivated all
    of this (fixtures aging past the cutoff, silently) could return unnoticed.
    """
    header = next(line for line in dump.splitlines() if line.startswith("# rows="))
    kept = int(header.split("ai_blogs=")[1].split()[0])
    assert kept > 0
    assert "arxiv_cs_ai=" in header
    assert "gh_trending=" in header


def test_every_source_contributes_rows(dump):
    """A source dropping to zero is the silent rot the health footer cannot catch."""
    header = next(line for line in dump.splitlines() if line.startswith("# rows="))
    for name in ("hn", "gh_trending", "hf_papers", "ai_blogs", "arxiv_cs_ai"):
        assert f"{name}=0 " not in header + " "
        assert f"{name}=" in header


def test_a_missing_fixture_fails_loudly(monkeypatch):
    """Not a quietly smaller snapshot: an unrouted URL is a broken harness."""
    from scripts import snapshot_pipeline

    monkeypatch.setattr(snapshot_pipeline, "ROUTES", {})
    with pytest.raises(SnapshotError, match="no fixture routed"):
        generate()
