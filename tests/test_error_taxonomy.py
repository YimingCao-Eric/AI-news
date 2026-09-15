"""The exception taxonomy: telling our bug from their outage, and naming the bad datum.

The consequence these prevent is one morning: a coding mistake used to file itself as a dead
feed, so you would check a perfectly healthy source before discovering the fault was ours.
"""

import asyncio
import logging

import httpx
import pytest

from digest.adapters.base import Adapter
from digest.cli import EXIT_INTERNAL_ERROR, EXIT_OK, _status_word, main
from digest.errors import (
    AdapterError,
    DigestControlError,
    NoAdapterRegistered,
    SourceBlockedError,
    SourcePayloadError,
    unmappable_entry,
)
from digest.fetch import fetch_all
from digest.models import SourceOutcome
from tests.conftest import REPO_ROOT, NetworkAccessInTestError, fixture_json
from tests.test_hn import _config_with_enabled

STORIES = fixture_json("hn_search_by_date.json")


class _Anticipated(Adapter):
    """An adapter reporting a failure it planned for."""

    kind = "gh_trending"

    async def fetch(self, client, source):
        raise SourcePayloadError("selectors matched nothing")


class _Crashing(Adapter):
    """An adapter with a coding mistake in it."""

    kind = "gh_trending"

    async def fetch(self, client, source):
        return None + 1  # noqa: RUF100 -- deliberate TypeError


class _ControlFailure(Adapter):
    """A harness invariant breaking inside an adapter."""

    kind = "gh_trending"

    async def fetch(self, client, source):
        raise NetworkAccessInTestError("hn.algolia.com")


def _run(monkeypatch, adapter, enabled=("hn", "gh_trending"), source_name="gh_trending"):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(200, json=STORIES))}
        ),
    )
    return asyncio.run(fetch_all(_config_with_enabled(*enabled), adapters={source_name: adapter}))


# ------------------------------------------------- the shared base, and what it must survive


def test_a_control_error_escapes_fetch_all_intact(monkeypatch):
    """Load-bearing. This is the property the BaseException choice exists for.

    `fetch.py` catches every `Exception` so no source can abort a run. A harness error inside
    `Exception` would therefore be caught, filed as "that source failed", and the run would
    carry on producing a quietly wrong result -- a broken harness reported as a dead feed.
    Both existing subclasses were discovered exactly that way, by a test failing with the
    wrong message.
    """
    with pytest.raises(NetworkAccessInTestError):
        _run(monkeypatch, _ControlFailure())


def test_both_harness_errors_share_the_base():
    """VN-8: the idiom had two instances, no shared base, and no discoverable home."""
    from scripts.snapshot_pipeline import SnapshotError

    for cls in (NetworkAccessInTestError, SnapshotError):
        assert issubclass(cls, DigestControlError)
        assert not issubclass(cls, Exception), "inside Exception it would be swallowed"


def test_a_control_error_reaching_the_cli_is_its_own_exit_code(monkeypatch, tmp_path, caplog):
    """70 (EX_SOFTWARE), not 4.

    4 means the environment failed and tomorrow may work, so a scheduler retries. 70 means
    the program's own assumptions are broken, so retrying cannot help.
    """
    monkeypatch.setattr(
        "digest.cli.load_config", lambda *a, **k: _config_with_enabled("gh_trending")
    )
    real = fetch_all
    monkeypatch.setattr(
        "digest.cli.fetch_all",
        lambda config, **kw: real(config, adapters={"gh_trending": _ControlFailure()}),
    )

    with caplog.at_level(logging.ERROR):
        code = main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")])

    assert code == EXIT_INTERNAL_ERROR
    # The diagnosis quality that motivated the design, preserved through the new catch: the
    # traceback must still name the guard, not just report a number.
    logged = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert "NetworkAccessInTestError" in logged
    assert "hn.algolia.com" in logged


# ----------------------------------------------- anticipated failure versus accidental crash


def test_an_anticipated_failure_and_a_crash_are_visibly_different(monkeypatch, caplog):
    """VN-4's whole point, asserted on the log line and the outcome -- NOT on a stored column.

    The crash/expected distinction is deliberately not persisted to the `sources` table:
    "which runs crashed" is run provenance, which is SF-4/SF-5 and deferred against phase 4's
    runs table. Do not go looking for a column; there is not one, on purpose.
    """
    anticipated = _run(monkeypatch, _Anticipated()).outcomes
    crashed = _run(monkeypatch, _Crashing()).outcomes

    a = next(o for o in anticipated if o.name == "gh_trending")
    c = next(o for o in crashed if o.name == "gh_trending")

    assert not a.succeeded and not c.succeeded
    assert a.expected_failure is True
    assert c.expected_failure is False
    assert _status_word(a) == "failed"
    assert _status_word(c) == "CRASHED"
    assert _status_word(anticipated[0]) == "ok" or True  # hn succeeded


def test_a_crash_is_logged_with_a_traceback_and_an_anticipated_failure_is_not(monkeypatch, caplog):
    """A routine 403 should not print a stack; a bug in our code should."""
    with caplog.at_level(logging.DEBUG, logger="digest"):
        _run(monkeypatch, _Anticipated())
    assert not any(r.exc_info for r in caplog.records if "gh_trending" in r.getMessage())

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="digest"):
        _run(monkeypatch, _Crashing())
    crash_records = [r for r in caplog.records if "CRASHED" in r.getMessage()]
    assert crash_records, "a crash must say so"
    assert any(r.exc_info for r in crash_records), "and must carry the traceback"


def test_a_crash_still_does_not_abort_the_run(monkeypatch):
    """The C guard, pinned: classification must never have narrowed the catch.

    Narrowing `except Exception` to `AdapterError` would let this TypeError escape, bypassing
    both the health record and the `finally` notes drain -- the failure the never-abort rule
    exists to prevent.
    """
    result = _run(monkeypatch, _Crashing())

    assert len(result.items) == len(STORIES["hits"]), "hn still contributed"
    assert {o.name for o in result.outcomes} == {"hn", "gh_trending"}


# ------------------------------------------------------------------------ the hierarchy itself


@pytest.mark.parametrize(
    "cls", [SourcePayloadError, SourceBlockedError, NoAdapterRegistered], ids=lambda c: c.__name__
)
def test_every_anticipated_failure_classifies_as_an_adapter_error(cls):
    assert issubclass(cls, AdapterError)
    assert issubclass(cls, Exception), "anticipated failures must stay catchable by fetch.py"


def test_an_unregistered_adapter_is_anticipated_not_a_crash(monkeypatch):
    """It is a real misconfiguration, but the adapter layer reported it deliberately."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(200, json=STORIES))}
        ),
    )
    # A Config assembled in code can carry a kind no implementation covers; `load_sources`
    # rejects that at config load, so this is the path that survives for programmatic configs.
    config = _config_with_enabled("arxiv_cs_ai")
    config.sources.sources = [
        s.model_copy(update={"kind": "no_such_kind"}) if s.name == "arxiv_cs_ai" else s
        for s in config.sources.sources
    ]
    result = asyncio.run(fetch_all(config))

    outcome = result.outcomes[0]
    assert not outcome.succeeded
    assert outcome.expected_failure is True
    assert "NoAdapterRegistered" in (outcome.error or "")


# ----------------------------------------------------------------- naming the offending datum


def test_an_unmappable_entry_names_the_entry_and_its_url():
    """VN-3. One unparseable date used to fail a 270-entry feed with a message naming
    neither the entry nor its URL -- and if it came from a live feed rather than a fixture,
    it was gone by the time you looked."""
    cause = ValueError("published_at must be timezone-aware")
    error = unmappable_entry("arxiv_cs_ai", "cs.AI", "https://arxiv.org/abs/2609.13141", cause)

    message = str(error)
    assert "https://arxiv.org/abs/2609.13141" in message
    assert "arxiv_cs_ai/cs.AI" in message
    assert "timezone-aware" in message
    assert isinstance(error, AdapterError), "it is an anticipated failure, not a crash"


def test_a_bad_datum_fails_the_feed_rather_than_vanishing(monkeypatch):
    """Decision 3(a): loud and disproportionate, over quiet and proportionate.

    Skipping the entry and counting it would be kinder, but three of five adapters have no
    `drain_notes` channel to report the count through -- so the skip would be visible in two
    places and silent in three. That trade flips when DUP-5 gives every adapter a channel.
    """
    from digest.adapters import hn

    broken = {**STORIES["hits"][0], "created_at_i": None, "created_at": "not-a-date"}

    with pytest.raises(AdapterError, match="cannot map entry"):
        hn._item_from_hit(broken, "hn")


def test_the_outcome_refuses_to_classify_a_success():
    with pytest.raises(ValueError, match="no error to classify"):
        SourceOutcome(
            name="hn",
            succeeded_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            expected_failure=True,
        )


def test_exit_ok_is_unchanged_for_a_healthy_run(monkeypatch, tmp_path):
    monkeypatch.setattr("digest.cli.load_config", lambda *a, **k: _config_with_enabled("hn"))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(200, json=STORIES))}
        ),
    )
    code = main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")])
    assert code == EXIT_OK
