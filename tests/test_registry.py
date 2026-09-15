"""The registry: implementation identity, source identity, and one instance per source.

The consequence these prevent: registering one adapter under two source names used to share
a single instance's `_notes` between two concurrently-fetched sources, attributing one
source's per-feed diagnostics to the other with no exception and no failing test.
"""

import asyncio

import httpx
import pytest

from digest.adapters.base import Adapter
from digest.config import ConfigError, load_config
from digest.errors import NoAdapterRegistered
from digest.fetch import IMPLEMENTATIONS, KNOWN_KINDS, build_adapters, fetch_all
from digest.models import Item
from tests.conftest import REPO_ROOT, load_repo_config
from tests.test_hn import STORIES, _config_with_enabled


class _Noting(Adapter):
    """Records which source it was asked for, then reports it as a note."""

    kind = "noting"

    async def fetch(self, client: httpx.AsyncClient, source) -> list[Item]:
        self._notes = [f"served {source.name}"]
        return []

    def drain_notes(self) -> list[str]:
        notes, self._notes = getattr(self, "_notes", []), []
        return notes


# ------------------------------------------------ LC-4: instances are private per source


def test_one_implementation_under_two_sources_gets_two_instances():
    """The landmine LC-2's natural fix would otherwise arm.

    The registry used to hold instances keyed by source name, so serving a second source
    with the same behaviour meant registering the *same object* twice. Two concurrent
    coroutines then shared `self._notes`: whichever drained first got a mixture, and the
    footer showed one source's feed diagnostics under the other. No exception, no failure.
    """
    config = _config_with_enabled("hn", "gh_trending")
    config.sources.sources = [
        s.model_copy(update={"kind": "noting"}) for s in config.sources.sources
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(IMPLEMENTATIONS, "noting", _Noting)
        built = build_adapters(config.sources.enabled, None)

    assert len(built) == 2
    first, second = built["hn"], built["gh_trending"]
    assert first is not second, "two sources must not share one instance"
    assert type(first) is type(second), "...while still being the same implementation"


def test_two_sources_sharing_an_implementation_keep_separate_notes(monkeypatch):
    """The same property, end to end, where the corruption would actually have shown."""
    config = _config_with_enabled("hn", "gh_trending")
    config.sources.sources = [
        s.model_copy(update={"kind": "noting"}) for s in config.sources.sources
    ]
    monkeypatch.setitem(IMPLEMENTATIONS, "noting", _Noting)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(200, json={}))}
        ),
    )
    result = asyncio.run(fetch_all(config))

    assert result.notes["hn"] == ["served hn"]
    assert result.notes["gh_trending"] == ["served gh_trending"]


def test_each_run_builds_fresh_instances(monkeypatch):
    """Per run, not per process: one run's notes cannot leak into the next."""
    config = _config_with_enabled("hn")
    first = build_adapters(config.sources.enabled, None)
    second = build_adapters(config.sources.enabled, None)

    assert first["hn"] is not second["hn"]


# --------------------------------------------------------- LC-2: kind selects, name identifies


def test_the_registry_is_keyed_by_implementation_not_by_source():
    """`kind` names the implementation; `name` names the source.

    Keyed by source name, one class could serve exactly one source -- a second RSS source
    needed a subclass whose only content was a different name, and PLAN.md section 2 Tier 2
    is mostly more RSS.
    """
    assert set(IMPLEMENTATIONS) == {"hn", "gh_trending", "hf_papers", "ai_blogs", "arxiv"}
    for kind, cls in IMPLEMENTATIONS.items():
        assert cls.kind == kind
        assert isinstance(cls, type), "the registry holds classes, not instances"


def test_every_shipped_source_names_an_implementation_that_exists():
    for source in load_repo_config().sources.sources:
        assert source.kind in IMPLEMENTATIONS


def test_kind_and_name_are_separate_fields_even_when_equal():
    """They coincide today because each implementation serves one source. That is a fact
    about today, not an identity -- `arxiv_cs_ai` already differs from kind `arxiv`."""
    by_name = {s.name: s for s in load_repo_config().sources.sources}
    assert by_name["arxiv_cs_ai"].kind == "arxiv"
    assert by_name["arxiv_cs_ai"].kind != by_name["arxiv_cs_ai"].name


def test_adapters_override_merges_rather_than_replaces():
    """A test pinning one source must not have to supply the other four."""
    config = _config_with_enabled("hn", "gh_trending", "hf_papers")
    injected = _Noting()

    built = build_adapters(config.sources.enabled, {"hn": injected})

    assert built["hn"] is injected
    assert built["gh_trending"] is not None
    assert built["hf_papers"] is not None
    assert len(built) == 3


# ------------------------------------------------------------------- unknown kinds fail loudly


def test_a_typo_in_kind_fails_at_config_load(tmp_path):
    """Not at fetch time -- by which point four healthy sources have already been fetched
    and the fifth looks like an outage."""
    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - name: hn\n"
        "    kind: json_api\n"  # the old category value: no longer an implementation
        "    url: https://example.com\n"
        "    weight: 1.0\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    (tmp_path / "interests.yaml").write_text(
        (REPO_ROOT / "interests.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="which no adapter implements"):
        load_config(tmp_path, known_kinds=KNOWN_KINDS)


def test_the_load_error_names_the_valid_kinds(tmp_path):
    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - name: hn\n"
        "    kind: nonsense\n"
        "    url: https://example.com\n"
        "    weight: 1.0\n",
        encoding="utf-8",
    )
    (tmp_path / "interests.yaml").write_text(
        (REPO_ROOT / "interests.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path, known_kinds=KNOWN_KINDS)

    message = str(excinfo.value)
    assert "nonsense" in message
    for kind in IMPLEMENTATIONS:
        assert kind in message


def test_known_kinds_is_injected_not_optional():
    """A caller who forgets gets a TypeError, never silently unvalidated config.

    Injected rather than imported because `adapters/base.py` imports `Source` from `config`,
    so reaching back would be a cycle -- and injecting keeps one source of truth instead of a
    second frozenset literal pinned by a test.
    """
    with pytest.raises(TypeError):
        load_config(REPO_ROOT)  # type: ignore[call-arg]


def test_an_unimplemented_kind_is_recorded_as_a_failure_not_skipped(monkeypatch):
    """Survives the restructuring: a source that cannot run is a failure, never absent.

    Reachable only for a Config assembled in code, since `load_sources` now rejects an
    unknown kind at load -- but silence here would still look like a quiet source.
    """
    config = _config_with_enabled("hn")
    config.sources.sources = [
        s.model_copy(update={"kind": "no_such_kind"}) for s in config.sources.sources
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

    assert [o.name for o in result.outcomes] == ["hn"], "recorded, not skipped"
    assert not result.outcomes[0].succeeded
    assert result.outcomes[0].expected_failure is True
    assert NoAdapterRegistered.__name__ in (result.outcomes[0].error or "")
