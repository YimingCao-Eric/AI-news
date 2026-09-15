"""Load and validate `sources.yaml` and `interests.yaml`.

Both files are resolved against the current working directory by default, with an explicit
`--config-dir` override on the CLI. Nothing here walks up from `__file__`: that trick breaks
the day the package is installed somewhere other than the checkout.

Every failure mode -- missing file, malformed YAML, unknown key, wrong type -- raises
`ConfigError` with a message that names the file and the offending field. `extra="forbid"`
throughout, so a typo'd key is an error rather than a setting that silently does nothing.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SOURCES_FILENAME = "sources.yaml"
INTERESTS_FILENAME = "interests.yaml"


class ConfigError(Exception):
    """Raised for any problem loading or validating a config file."""


# --------------------------------------------------------------------------- sources.yaml


class Feed(BaseModel):
    """One endpoint inside a bundle source. See `Source.feeds`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    stale_after_days: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Days without a new entry before this feed is reported STALE in the run "
            "summary. Per feed rather than global: a feed that posts monthly is not stale "
            "at 40 days, and one threshold for all of them either cries wolf on the slow "
            "feeds or sleeps through the fast ones -- and a warning that cries wolf gets "
            "ignored, which costs the whole mechanism. None uses "
            "adapters.ai_blogs.DEFAULT_STALENESS_THRESHOLD_DAYS."
        ),
    )


class Source(BaseModel):
    """One configured source. Endpoints come from docs/PLAN.md section 2, Tier 1.

    A source has *either* a single `url` or a list of `feeds`, never both and never
    neither. `ai_blogs` is the bundle case: six separate RSS endpoints that share one
    weight, one fetch budget and one health record. Adapters branch on `feeds is None`
    rather than on `kind`, since the field's presence is what the validator guarantees.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    #: Which adapter implementation serves this source -- NOT what the source runs on.
    #:
    #: `name` identifies the source, `kind` identifies the implementation. They coincide for
    #: all five sources today because each implementation serves exactly one source, and that
    #: redundancy is left visible rather than hidden: the first divergence is a second RSS
    #: source declaring an existing kind, which is the moment to rename the implementation.
    #:
    #: `kind` never encodes *shape*. Whether a source has one endpoint or six is determined
    #: by field presence (`url` vs `feeds`) and normalised by `endpoints`, exactly as phase 0
    #: decided -- adapters iterate `source.endpoints` and never ask how many. This field was
    #: previously a category (`json_api`, `rss`, `html`) that dispatched nothing, and those
    #: values could not become the dispatch key: `json_api` and `rss` each covered two
    #: unrelated adapters.
    kind: str
    url: str | None = None
    feeds: list[Feed] | None = None
    weight: float
    fetch_limit: int | None = Field(
        default=None,
        ge=1,
        description="Max items to pull and store per run; None means unlimited. "
        "Distinct from Topic.quota, which caps how many reach the digest.",
    )
    enabled: bool = True

    @model_validator(mode="after")
    def _exactly_one_endpoint_shape(self) -> "Source":
        if (self.url is None) == (self.feeds is None):
            raise ValueError(
                f"source {self.name!r} must set exactly one of `url` or `feeds`, not "
                f"{'both' if self.url is not None else 'neither'}."
            )
        if self.feeds is not None:
            if not self.feeds:
                raise ValueError(f"source {self.name!r} has an empty `feeds` list.")
            names = [f.name for f in self.feeds]
            duplicates = sorted({n for n in names if names.count(n) > 1})
            if duplicates:
                raise ValueError(
                    f"source {self.name!r} has duplicate feed name(s): {', '.join(duplicates)}"
                )
        return self

    @property
    def endpoints(self) -> list[Feed]:
        """Every endpoint this source fetches, bundle or not, as a uniform list."""
        if self.feeds is not None:
            return list(self.feeds)
        assert self.url is not None  # guaranteed by _exactly_one_endpoint_shape
        return [Feed(name=self.name, url=self.url)]


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[Source]

    @property
    def enabled(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]

    def by_name(self, name: str) -> Source:
        for source in self.sources:
            if source.name == name:
                return source
        known = ", ".join(s.name for s in self.sources)
        raise ConfigError(f"No source named {name!r} in {SOURCES_FILENAME}. Known: {known}")


# ------------------------------------------------------------------------- interests.yaml


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    weight: float
    quota: int = Field(ge=0, description="Max items of this topic that may reach the digest.")
    keywords: list[str] = Field(default_factory=list)
    anti_keywords: list[str] = Field(default_factory=list)
    lenses: list[str] = Field(default_factory=list)
    note: str | None = None


class HardRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    always_include: list[str] = Field(default_factory=list)
    max_items_per_digest: int = Field(ge=1)
    max_summarised: int = Field(ge=0)
    min_hn_points: int = Field(ge=0)
    max_age_hours: int = Field(ge=1)


class InterestProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str
    topics: list[Topic]
    anti_topics: list[str] = Field(default_factory=list)
    hard_rules: HardRules


class Config(BaseModel):
    """Both config files, loaded together."""

    model_config = ConfigDict(extra="forbid")

    sources: SourcesConfig
    interests: InterestProfile
    config_dir: Path


# ---------------------------------------------------------------------------- the loading


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            f"Expected {path.name} in the config directory. Run from the repo root, "
            f"or pass --config-dir /path/to/repo."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML:\n{exc}") from exc
    if raw is None:
        raise ConfigError(f"{path} is empty.")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} must contain a YAML mapping at the top level, got {type(raw).__name__}."
        )
    return raw


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"{path} failed validation ({exc.error_count()} problem(s)):"]
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {location}: {err['msg']}")
    return "\n".join(lines)


def _flatten_hard_rules(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    """Collapse `hard_rules` from a list of single-key mappings into one mapping.

    docs/interests.md writes it as a YAML list so it reads as a checklist; the model wants a
    mapping. Converting here keeps interests.yaml matching the doc verbatim.
    """
    rules = raw.get("hard_rules")
    if not isinstance(rules, list):
        return raw

    flattened: dict[str, Any] = {}
    for entry in rules:
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{path}: every hard_rules entry must be a `key: value` mapping, got {entry!r}."
            )
        for key, value in entry.items():
            if key in flattened:
                raise ConfigError(f"{path}: hard_rules sets {key!r} more than once.")
            flattened[key] = value
    return {**raw, "hard_rules": flattened}


def load_sources(config_dir: Path | None = None, *, known_kinds: frozenset[str]) -> SourcesConfig:
    """Load and validate sources.yaml.

    `known_kinds` is injected rather than imported, and is required rather than optional.

    Injected because `config` cannot import the adapter registry: `adapters/base.py` imports
    `Source` from here, so reaching the other way is a cycle. The alternative -- a second
    frozenset literal in this module, pinned to the registry by a test -- would be two
    sources of truth for one fact. This is the same injection the project already chose for
    the clock (`AIBlogsAdapter(clock=...)`) and for adapters themselves
    (`fetch_all(adapters=...)`); a third mechanism for the same concern is the shape VN-10
    flagged once already.

    Required because a caller who forgets should get a `TypeError`, not silently unvalidated
    config -- the same reasoning that makes `kind` itself required rather than defaulted.
    """
    path = resolve_config_dir(config_dir) / SOURCES_FILENAME
    raw = _read_yaml(path)
    try:
        config = SourcesConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc

    # Checked here rather than on the model so the message can name the valid set, and so a
    # typo fails at load rather than at fetch time -- by which point four healthy sources
    # have already been fetched and the fifth looks like an outage.
    for source in config.sources:
        if source.kind not in known_kinds:
            raise ConfigError(
                f"{path}: source {source.name!r} has kind {source.kind!r}, which no adapter "
                f"implements. Valid kinds: {', '.join(sorted(known_kinds))}."
            )

    names = [s.name for s in config.sources]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"{path}: duplicate source name(s): {', '.join(duplicates)}")
    return config


def load_interests(config_dir: Path | None = None) -> InterestProfile:
    path = resolve_config_dir(config_dir) / INTERESTS_FILENAME
    raw = _flatten_hard_rules(_read_yaml(path), path)
    try:
        profile = InterestProfile.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc

    names = [t.name for t in profile.topics]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"{path}: duplicate topic name(s): {', '.join(duplicates)}")
    return profile


def resolve_config_dir(config_dir: Path | None = None) -> Path:
    """Where the YAML lives: the `--config-dir` override, else the current directory."""
    return (config_dir or Path.cwd()).expanduser().resolve()


def load_config(config_dir: Path | None = None, *, known_kinds: frozenset[str]) -> Config:
    """Both config files. See `load_sources` for why `known_kinds` is injected and required."""
    resolved = resolve_config_dir(config_dir)
    return Config(
        sources=load_sources(resolved, known_kinds=known_kinds),
        interests=load_interests(resolved),
        config_dir=resolved,
    )


def summarise_config(config: Config) -> str:
    """A human-readable dump of what got loaded, for the CLI's placeholder subcommands."""
    lines = [f"config dir: {config.config_dir}", ""]

    enabled = config.sources.enabled
    lines.append(f"sources ({len(enabled)}/{len(config.sources.sources)} enabled):")
    for source in config.sources.sources:
        mark = "on " if source.enabled else "off"
        limit = "unlimited" if source.fetch_limit is None else str(source.fetch_limit)
        lines.append(
            f"  [{mark}] {source.name:<12} {source.kind:<9} weight={source.weight:<4} "
            f"fetch_limit={limit}"
        )
        if source.feeds is not None:
            feed_names = ", ".join(f.name for f in source.feeds)
            lines.append(f"           {len(source.feeds)} feeds: {feed_names}")

    interests = config.interests
    total_quota = sum(t.quota for t in interests.topics)
    lines += [
        "",
        f"interests: {len(interests.topics)} topics, quota total {total_quota}, "
        f"{len(interests.anti_topics)} anti-topics",
    ]
    for topic in interests.topics:
        lines.append(
            f"  {topic.name:<34} weight={topic.weight:<4} quota={topic.quota:<3} "
            f"{len(topic.keywords)} keywords"
        )

    rules = interests.hard_rules
    lines += [
        "",
        f"hard rules: max_items_per_digest={rules.max_items_per_digest} "
        f"max_summarised={rules.max_summarised} min_hn_points={rules.min_hn_points} "
        f"max_age_hours={rules.max_age_hours}",
    ]
    return "\n".join(lines)
