"""Unattended-run signals: what a scheduler and a half-awake reader can tell at 07:00.

These cover the states that used to be indistinguishable. The consequence they prevent is
concrete: every source failing exited 0, so seven consecutive total failures would have
satisfied PLAN.md section 0's "runs unattended for seven consecutive days" criterion while
delivering nothing.
"""

import logging
from datetime import UTC, datetime

import httpx

from digest.cli import (
    EXIT_ALL_SOURCES_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_STORAGE,
    EXIT_USAGE_OR_CONFIG,
    _health_summary,
    main,
)
from digest.fetch import fetch_all
from digest.models import SourceHealth
from tests.conftest import REPO_ROOT, fixture_json
from tests.test_hn import _config_with_enabled, _Exploding

STORIES = fixture_json("hn_search_by_date.json")


def _serve(monkeypatch, payload=None, enabled=("hn",), adapters=None):
    """Point the CLI at a mocked transport, chosen sources, and injected adapters.

    `adapters` merges: naming one source leaves the others constructing normally.
    """
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload if payload is not None else STORIES)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handler)}),
    )
    monkeypatch.setattr("digest.cli.load_config", lambda *a, **k: _config_with_enabled(*enabled))
    if adapters is not None:
        real = fetch_all
        monkeypatch.setattr(
            "digest.cli.fetch_all", lambda config, **kw: real(config, adapters=adapters)
        )


# ------------------------------------------------------------------------------ exit codes


def test_every_source_failing_exits_non_zero(monkeypatch, tmp_path, capsys):
    """The headline. A scheduler decides "did it work?" from this alone."""
    _serve(
        monkeypatch,
        enabled=("hn", "gh_trending"),
        adapters={"hn": _Exploding(), "gh_trending": _Exploding()},
    )

    code = main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")])
    assert code == EXIT_ALL_SOURCES_FAILED
    assert code != EXIT_OK


def test_one_source_failing_still_exits_zero(monkeypatch, tmp_path):
    """Partial failure is 0 on purpose.

    Sources fail routinely, and a code that fires most mornings gets filtered -- the same
    reasoning that gave ai_blogs per-feed staleness thresholds rather than one global number.
    It also must stay 0 because CLAUDE.md forbids one dead source failing the job.
    """
    _serve(monkeypatch, enabled=("hn", "gh_trending"), adapters={"gh_trending": _Exploding()})

    code = main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")])
    assert code == EXIT_OK


def test_an_interrupt_is_not_the_all_sources_failed_code(monkeypatch, tmp_path, capsys):
    """Phase 1 established an operator quitting is not source rot. That has to reach $?.

    Otherwise a scheduler reads Ctrl-C as "every source died" and escalates.
    """

    async def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("digest.cli.fetch_all", interrupt)
    _serve(monkeypatch)

    code = main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")])
    assert code == EXIT_INTERRUPTED
    assert code != EXIT_ALL_SOURCES_FAILED
    assert "interrupted" in capsys.readouterr().err


def test_the_exit_codes_are_all_distinct():
    """The states a scheduler must tell apart cannot collapse into one number."""
    codes = [
        EXIT_OK,
        EXIT_USAGE_OR_CONFIG,
        EXIT_STORAGE,
        EXIT_ALL_SOURCES_FAILED,
        EXIT_INTERRUPTED,
    ]
    assert len(set(codes)) == len(codes)


# ------------------------------------------------------------------- the run log's visibility


def test_the_per_source_line_appears_without_v(monkeypatch, tmp_path, caplog):
    """CLAUDE.md mandates one line per source per run. Nobody types -v in a crontab.

    It sat at INFO under a WARNING default, so the scheduled 07:00 run wrote it nowhere --
    the one artefact that would have shown a source quietly returning zero for a week.
    """
    _serve(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="digest"):
        main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")])

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("source=")]
    assert lines, "the mandated per-source line was not emitted"
    assert "fetched=" in lines[0] and "new=" in lines[0] and "duration=" in lines[0]


def test_the_digest_logger_is_raised_but_the_root_is_not(monkeypatch, tmp_path):
    """Raising the root instead would drag in httpx's per-request chatter.

    A line buried in noise is barely better than a missing one.
    """
    _serve(monkeypatch)
    main(["fetch", "--config-dir", str(REPO_ROOT), "--db", str(tmp_path / "t.db")])

    assert logging.getLogger("digest").level == logging.INFO
    assert logging.getLogger().level == logging.WARNING


# --------------------------------------------------------------------------- render guard


def test_render_against_a_missing_database_does_not_create_one(tmp_path, capsys):
    """A typo'd --db used to yield a brand-new file and a confident "Nothing new"."""
    missing = tmp_path / "typo.db"

    code = main(["render", "--config-dir", str(REPO_ROOT), "--db", str(missing)])

    assert code == EXIT_STORAGE
    assert not missing.exists(), "render created the database it was supposed to read"
    assert "will not create one" in capsys.readouterr().err


# ---------------------------------------------------------------------------- footer states


def _health(name, **kwargs) -> SourceHealth:
    return SourceHealth(name=name, **kwargs)


def test_the_footer_distinguishes_every_source_state():
    """Five states, five shapes. Several used to render identically."""
    ok_at = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
    failed_at = datetime(2026, 9, 14, 7, 1, tzinfo=UTC)
    health = [
        _health("hn", last_success_at=ok_at),
        _health("hf_papers", last_success_at=ok_at),
        _health(
            "gh_trending",
            last_success_at=ok_at,
            consecutive_failures=1,
            total_failures=3,
            last_failure_at=failed_at,
        ),
        _health("arxiv_cs_ai", last_success_at=ok_at),
    ]
    items = [_item("hn")]

    text = _health_summary(items, health, enabled={"hn", "hf_papers", "gh_trending"})
    by_source = {line.split()[0]: line for line in text.splitlines() if line.startswith("  ")}

    assert " ok " in by_source["hn"]  # ran, returned items
    assert " quiet " in by_source["hf_papers"]  # ran, returned nothing
    assert " FAILED " in by_source["gh_trending"]  # failed this run
    assert " disabled " in by_source["arxiv_cs_ai"]  # not enabled at all
    assert "items=—" in by_source["arxiv_cs_ai"], "a disabled source has no count to report"


def test_quiet_and_disabled_are_not_the_same_line():
    """The specific collision SF-7 names: both used to print items=0 and 'ok'."""
    ok_at = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
    health = [_health("hn", last_success_at=ok_at), _health("arxiv_cs_ai", last_success_at=ok_at)]

    text = _health_summary([], health, enabled={"hn"})
    hn_line, arxiv_line = (line for line in text.splitlines() if line.startswith("  "))

    assert hn_line.split()[1] != arxiv_line.split()[1]


def test_failure_history_reads_unambiguously():
    """`consecutive=1 total=3`, not `1 of 3 total`, which parses as "1 out of 3 attempts"."""
    text = _health_summary(
        [],
        [
            _health(
                "gh_trending",
                consecutive_failures=1,
                total_failures=3,
                last_failure_at=datetime(2026, 9, 14, 7, 1, tzinfo=UTC),
            )
        ],
        enabled={"gh_trending"},
    )
    assert "consecutive=1" in text
    assert "total=3" in text
    assert "last_failure=2026-09-14T07:01:00" in text


def test_a_pre_migration_row_is_marked_as_a_floor_not_a_total():
    """`total_failures < consecutive_failures` proves the row predates counting.

    The v2 migration back-fills `total_failures` as 0, so a long-failing source would read
    `consecutive=2 total=0` -- a self-contradiction, and a number that under-reports a health
    signal. `≥` states a floor instead of inventing a figure, needs no column and no seeding,
    and disappears by itself once the true count overtakes.
    """
    text = _health_summary(
        [], [_health("hn", consecutive_failures=2, total_failures=0)], enabled={"hn"}
    )
    assert "consecutive=2" in text
    assert "total=≥2" in text
    assert "total=0" not in text


def test_a_counted_row_carries_no_floor_marker():
    text = _health_summary(
        [], [_health("hn", consecutive_failures=2, total_failures=5)], enabled={"hn"}
    )
    assert "total=5" in text
    assert "≥" not in text


def _item(source: str):
    from digest.models import Item

    return Item(
        url=f"https://example.com/{source}",
        title="A thing",
        source=source,
        author=None,
        published_at=None,
        raw={},
    )
