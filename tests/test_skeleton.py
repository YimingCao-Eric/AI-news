"""Phase 0 smoke tests: the models enforce what they claim and the shipped config parses.

No network, no fixtures needed yet -- adapter tests against recorded fixtures arrive in
phase 1.
"""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from digest.cli import build_parser, main
from digest.config import ConfigError, Source, load_config
from digest.models import Item, SourceHealth

REPO_ROOT = Path(__file__).resolve().parents[1]


def _item(**overrides):
    base = dict(
        url="https://example.com/a",
        title="A thing shipped",
        source="hn",
        author=None,
        published_at=None,
        raw={},
    )
    return Item(**{**base, **overrides})


def test_item_defaults_are_none():
    item = _item()
    assert (item.topic, item.score, item.score_reason, item.summary) == (None, None, None, None)


def test_item_rejects_naive_published_at():
    with pytest.raises(ValidationError, match="timezone-aware"):
        _item(published_at=datetime(2026, 9, 12, 7, 0, 0))


@pytest.mark.parametrize(
    "tz",
    [UTC, timezone(timedelta(hours=-7)), timezone(timedelta(hours=5, minutes=30))],
    ids=["utc", "vancouver-offset", "half-hour-offset"],
)
def test_item_accepts_aware_published_at(tz):
    item = _item(published_at=datetime(2026, 9, 12, 7, 0, 0, tzinfo=tz))
    assert item.published_at.tzinfo is not None


def test_item_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        _item(first_seen_at="2026-09-12T07:00:00Z")


def test_source_health_rejects_naive_last_success():
    with pytest.raises(ValidationError, match="timezone-aware"):
        SourceHealth(name="hn", last_success_at=datetime(2026, 9, 12))


def test_source_health_defaults():
    health = SourceHealth(name="hn")
    assert health.last_success_at is None
    assert health.consecutive_failures == 0


def test_shipped_config_loads():
    config = load_config(REPO_ROOT)
    assert [s.name for s in config.sources.sources] == [
        "hn",
        "gh_trending",
        "hf_papers",
        "ai_blogs",
        "arxiv_cs_ai",
    ]
    assert [s.name for s in config.sources.enabled] == ["hn"]
    assert config.sources.by_name("arxiv_cs_ai").fetch_limit == 50
    assert config.sources.by_name("ai_blogs").fetch_limit is None
    assert config.interests.hard_rules.min_hn_points == 100
    assert len(config.interests.topics) == 13


def test_ai_blogs_is_a_six_feed_bundle():
    bundle = load_config(REPO_ROOT).sources.by_name("ai_blogs")
    assert [f.name for f in bundle.feeds] == [
        "openai_news",
        "deepmind",
        "anthropic_news",
        "anthropic_engineering",
        "meta_ai",
        "mistral",
    ]
    assert bundle.url is None
    assert len(bundle.endpoints) == 6


def test_single_url_source_exposes_one_endpoint():
    hn = load_config(REPO_ROOT).sources.by_name("hn")
    assert [e.name for e in hn.endpoints] == ["hn"]
    assert hn.endpoints[0].url == hn.url


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ({}, "neither"),
        ({"url": "https://a", "feeds": [{"name": "x", "url": "https://b"}]}, "both"),
        ({"feeds": []}, "empty"),
        (
            {"feeds": [{"name": "x", "url": "https://a"}, {"name": "x", "url": "https://b"}]},
            "duplicate feed",
        ),
    ],
    ids=["neither", "both", "empty-bundle", "duplicate-feed-names"],
)
def test_source_endpoint_shape_is_validated(extra, match):
    with pytest.raises(ValidationError, match=match):
        Source(name="s", kind="rss", weight=1.0, **extra)


def test_missing_config_dir_fails_loudly(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path)


def test_unknown_key_fails_loudly(tmp_path):
    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - name: hn\n"
        "    kind: json_api\n"
        "    url: https://example.com\n"
        "    weight: 1.0\n"
        "    quota: 30\n"  # the old name -- must not be silently accepted
        "    enabled: true\n",
        encoding="utf-8",
    )
    (tmp_path / "interests.yaml").write_text("profile: x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="quota"):
        load_config(tmp_path)


@pytest.mark.parametrize("command", ["fetch", "rank", "render", "run"])
def test_every_subcommand_parses(command):
    assert build_parser().parse_args([command]).command == command


@pytest.mark.parametrize("command", ["rank", "render", "run"])
def test_stub_subcommands_still_print_the_config(command, capsys):
    """`fetch` is excluded deliberately: it is live as of phase 1 and would hit the network.

    Its CLI path is covered in test_hn.py with the transport mocked. This test used to
    include it and silently started making real HTTP calls the moment fetch was wired up --
    which is how a suite that claims to be offline stops being one.
    """
    assert main([command, "--config-dir", str(REPO_ROOT)]) == 0
    assert "not implemented" in capsys.readouterr().out


def test_no_subcommand_prints_help_and_fails():
    assert main([]) == 1


def test_bad_config_dir_exits_two(tmp_path, capsys):
    assert main(["fetch", "--config-dir", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err
