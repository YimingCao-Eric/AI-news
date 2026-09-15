"""SQLite storage, URL canonicalisation, dedupe, and the source-health counter."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from digest.models import Item, SourceOutcome
from digest.store import (
    SCHEMA_VERSION,
    StoreError,
    canonical_url,
    checkpoint,
    count_dupes,
    from_iso,
    get_source_health,
    init_db,
    insert_items,
    is_near_duplicate,
    record_source_health,
    stamp_digest_date,
    title_similarity,
    titles_are_near_duplicates,
    to_iso,
    unrendered_items,
    url_hash,
)
from tests.conftest import fixture_text


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def make_item(**overrides: Any) -> Item:
    base: dict[str, Any] = {
        "url": "https://example.com/a",
        "title": "A thing shipped",
        "source": "hn",
        "author": "someone",
        "published_at": datetime(2026, 9, 12, 14, 0, tzinfo=UTC),
        "raw": {"objectID": "1", "points": 120},
    }
    return Item(**{**base, **overrides})


# ------------------------------------------------------------------------- canonical_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # utm_* stripped, whatever the suffix
        (
            "https://example.com/post?utm_source=hn&utm_medium=social&id=7",
            "https://example.com/post?id=7",
        ),
        # other unambiguous tracking params
        ("https://example.com/p?fbclid=abc", "https://example.com/p"),
        ("https://example.com/p?gclid=abc&igshid=z&mc_cid=9", "https://example.com/p"),
        ("https://example.com/p?ref=hn&ref_src=twsrc", "https://example.com/p"),
        # trailing slash
        ("https://example.com/post/", "https://example.com/post"),
        ("https://example.com/", "https://example.com"),
        # fragment
        ("https://example.com/post#section-3", "https://example.com/post"),
        # uppercase scheme and host
        ("HTTPS://Example.COM/Post", "https://example.com/Post"),
        # leading www.
        ("https://www.example.com/post", "https://example.com/post"),
        # everything at once
        (
            "HTTPS://WWW.Example.com/Post/?utm_campaign=x&id=3#top",
            "https://example.com/Post?id=3",
        ),
        # ...and one that must come back untouched
        (
            "https://news.ycombinator.com/item?id=44567890",
            "https://news.ycombinator.com/item?id=44567890",
        ),
        ("https://arxiv.org/abs/2609.01234v2", "https://arxiv.org/abs/2609.01234v2"),
    ],
)
def test_canonical_url(raw, expected):
    assert canonical_url(raw) == expected


def test_path_case_is_preserved():
    """Hosts are case-insensitive; paths are not. Lowercasing a path can 404."""
    assert canonical_url("https://github.com/Olshansk/RSS-Feeds") == (
        "https://github.com/Olshansk/RSS-Feeds"
    )


def test_source_param_is_not_stripped():
    """Pinned decision: `source` is a normal filter on plenty of sites, not just tracking.

    Collapsing these would drop the second item via INSERT OR IGNORE -- no row, no log, and
    the only symptom is missing something you never knew existed. A false split just shows a
    story twice. The asymmetry is why `source` came off the strip list.
    """
    trending = "https://huggingface.co/models?source=trending"
    likes = "https://huggingface.co/models?source=likes"
    assert canonical_url(trending) != canonical_url(likes)
    assert url_hash(trending) != url_hash(likes)


def test_url_hash_is_stable_and_canonical():
    a = url_hash("https://WWW.Example.com/post/?utm_source=x#frag")
    b = url_hash("https://example.com/post")
    assert a == b
    assert len(a) == 64


# ----------------------------------------------------------------------------- timestamps


def test_to_iso_refuses_naive():
    with pytest.raises(StoreError, match="naive"):
        to_iso(datetime(2026, 9, 12, 14, 0))


def test_from_iso_refuses_naive():
    with pytest.raises(StoreError, match="no timezone"):
        from_iso("2026-09-12T14:00:00")


def test_datetime_round_trip_preserves_the_instant(conn):
    """A +05:30 source must come back aware and pointing at the same moment.

    It comes back as +00:00, not +05:30: the instant is preserved, the original offset is
    not, which is what "stored as ISO8601 UTC strings" means. What matters is that it is
    aware -- a naive value would be rejected by the Item validator, and loosening that
    validator would undo phase 0.
    """
    kolkata = timezone(timedelta(hours=5, minutes=30))
    published = datetime(2026, 9, 12, 14, 0, tzinfo=kolkata)

    insert_items(conn, [make_item(published_at=published)])
    (stored,) = unrendered_items(conn)

    assert stored.published_at is not None
    assert stored.published_at.tzinfo is not None
    assert stored.published_at == published
    assert stored.published_at.utcoffset() == timedelta(0)
    assert stored.published_at.hour == 8  # 14:00+05:30 is 08:30 UTC
    assert stored.published_at.minute == 30


def test_no_datetime_object_reaches_sqlite(conn):
    """Python 3.12 deprecates the default adapter; we convert at the boundary instead."""
    insert_items(conn, [make_item()])
    value = conn.execute("SELECT published_at FROM items").fetchone()["published_at"]
    assert isinstance(value, str)
    assert value.endswith("+00:00")


# --------------------------------------------------------------------------------- schema


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    init_db(path).close()
    conn = init_db(path)
    assert conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"] == 1
    conn.close()


def test_refuses_a_newer_schema(tmp_path):
    """A silent mismatch corrupts data; refuse loudly instead."""
    path = tmp_path / "future.db"
    init_db(path).close()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,))
    conn.close()

    with pytest.raises(StoreError, match="Refusing to touch it"):
        init_db(path)


def test_refuses_a_version_with_no_migration_path(tmp_path):
    """Older is now upgraded, not refused -- but only along a chain that actually exists.

    A gap must still fail loudly rather than be guessed at: version 0 has no step to 1, so
    running the v1->v2 step against it would apply the wrong DDL to an unknown shape.
    """
    path = tmp_path / "old.db"
    init_db(path).close()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("UPDATE schema_version SET version = 0")
    conn.close()

    with pytest.raises(StoreError, match="no migration to 1 exists"):
        init_db(path)


def test_checkpoint_leaves_the_wal_empty(tmp_path):
    """Phase 5 depends on this: a committed .db with a non-empty -wal is silently behind."""
    path = tmp_path / "wal.db"
    conn = init_db(path)
    insert_items(conn, [make_item()])

    wal = path.with_name(path.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "expected WAL mode to be active"

    checkpoint(conn)
    assert wal.stat().st_size == 0

    # ...and the data really is in the main file, readable by a fresh connection.
    conn.close()
    other = sqlite3.connect(path)
    assert other.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    other.close()


def test_sources_table_has_no_enabled_column(conn):
    """sources.yaml decides what runs. Two `enabled` flags would eventually disagree."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)")}
    assert "enabled" not in columns
    assert {"name", "last_success_at", "consecutive_failures", "etag"} <= columns


# ---------------------------------------------------------------------------------- upsert


def test_second_insert_of_the_same_url_adds_nothing(conn):
    items = [make_item(), make_item(url="https://example.com/b", title="Another thing")]
    assert insert_items(conn, items) == 2
    assert insert_items(conn, items) == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 2


def test_urls_differing_only_by_tracking_collapse(conn):
    assert insert_items(conn, [make_item(url="https://example.com/a")]) == 1
    assert insert_items(conn, [make_item(url="https://www.example.com/a/?utm_source=hn")]) == 0


def test_raw_json_is_the_source_payload_not_the_item(conn):
    """`raw` is the original payload; Item's other fields are already columns."""
    insert_items(conn, [make_item(raw={"objectID": "42", "points": 300, "nested": {"a": 1}})])
    stored = json.loads(conn.execute("SELECT raw_json FROM items").fetchone()["raw_json"])

    assert stored == {"objectID": "42", "points": 300, "nested": {"a": 1}}
    assert "title" not in stored
    assert "url" not in stored


def test_first_seen_at_is_one_timestamp_for_the_whole_batch(conn):
    """One run means one timestamp, or the dupe count scoped to it is fuzzy."""
    insert_items(conn, [make_item(url=f"https://example.com/{i}", title=f"T{i}") for i in range(5)])
    stamps = {row["first_seen_at"] for row in conn.execute("SELECT first_seen_at FROM items")}
    assert len(stamps) == 1


# -------------------------------------------------------------------------- near-duplicate

# The same announcement as two outlets wrote it: one adds "the" and "GPU".
TITLE_A = "Nvidia announces the RTX 5090 Super GPU at CES 2027"
TITLE_B = "Nvidia announces RTX 5090 Super at CES 2027"
TITLE_C = "Nvidia announces RTX 5090 Super at CES 2027 keynote"


def test_real_world_title_variants_are_near_duplicates():
    assert title_similarity(TITLE_A, TITLE_B) >= 0.85
    assert titles_are_near_duplicates(TITLE_A, TITLE_B)


def test_different_stories_are_not_near_duplicates():
    other = "AMD announces RX 9090 XT at CES 2027"
    assert title_similarity(TITLE_A, other) < 0.85
    assert not titles_are_near_duplicates(TITLE_A, other)


NUMERIC_ONLY_DIFFERENCES = [
    (
        "Nvidia announces RTX 5090 Super at CES 2027",
        "Nvidia announces RTX 5080 Super at CES 2027",
    ),
    ("Anthropic ships Claude Opus 4.5 today", "Anthropic ships Claude Opus 4.6 today"),
    ("Scaling laws for 7B parameter models", "Scaling laws for 8B parameter models"),
]


@pytest.mark.parametrize(
    ("left", "right"),
    NUMERIC_ONLY_DIFFERENCES,
    ids=["gpu-model", "version-number", "parameter-count"],
)
def test_titles_differing_only_by_a_number_are_not_duplicates(left, right):
    """This digest's subject matter is version numbers and model names.

    Two different product announcements whose titles differ by one digit are the most likely
    false positive by a distance, so identical numeric tokens is a hard requirement on top of
    the ratio.
    """
    assert not titles_are_near_duplicates(left, right)


@pytest.mark.parametrize(
    ("left", "right"), NUMERIC_ONLY_DIFFERENCES[:2], ids=["gpu-model", "version-number"]
)
def test_the_numeric_guard_is_load_bearing(left, right):
    """Proof the guard earns its place: Dice alone would have called these duplicates."""
    assert title_similarity(left, right) >= 0.85


def test_numeric_guard_covers_alphanumeric_tokens():
    """`7b` and `v2` are not `str.isdigit()`, but they are exactly what must not collapse."""
    assert not titles_are_near_duplicates(
        "Qwen 3 7B instruct weights released", "Qwen 3 8B instruct weights released"
    )
    assert not titles_are_near_duplicates(
        "MOSS Transcribe Diarize v2 benchmarks", "MOSS Transcribe Diarize v3 benchmarks"
    )


def test_inflectional_variants_are_a_known_miss():
    """Documented limitation: `releases` and `released` are different tokens.

    Fixing it needs stemming, which is a dependency or a pile of rules. Pinned so the
    behaviour is a known gap rather than a surprise.
    """
    assert not titles_are_near_duplicates(
        "Anthropic releases Claude Opus 4.5", "Claude Opus 4.5 released by Anthropic"
    )


def test_near_duplicates_are_stored_and_flagged_not_dropped(conn):
    first = make_item(url="https://one.example/a", title=TITLE_A)
    second = make_item(url="https://two.example/b", title=TITLE_B, source="ai_blogs")

    assert insert_items(conn, [first]) == 1
    assert insert_items(conn, [second]) == 1  # stored, not dropped

    rows = {row["url"]: row["dupe_of"] for row in conn.execute("SELECT url, dupe_of FROM items")}
    assert rows["https://one.example/a"] is None
    assert rows["https://two.example/b"] == url_hash("https://one.example/a")


def test_near_duplicate_respects_the_window(conn):
    old = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    insert_items(conn, [make_item(url="https://one.example/a", title=TITLE_A)], now=old)

    later = old + timedelta(days=10)
    candidate = make_item(url="https://two.example/b", title=TITLE_B)
    assert is_near_duplicate(conn, candidate, window_days=3, now=later) is None
    assert is_near_duplicate(conn, candidate, window_days=30, now=later) is not None


def test_dupe_chains_point_at_the_original(conn):
    """A third copy references the first, not the second."""
    insert_items(conn, [make_item(url="https://one.example/a", title=TITLE_A)])
    insert_items(conn, [make_item(url="https://two.example/b", title=TITLE_B)])
    insert_items(conn, [make_item(url="https://three.example/c", title=TITLE_C)])

    rows = {row["url"]: row["dupe_of"] for row in conn.execute("SELECT url, dupe_of FROM items")}
    assert rows["https://three.example/c"] == url_hash("https://one.example/a")


def test_count_dupes_is_scoped_to_the_run(conn):
    monday = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    tuesday = monday + timedelta(days=1)

    insert_items(conn, [make_item(url="https://one.example/a", title=TITLE_A)], now=monday)
    insert_items(conn, [make_item(url="https://two.example/b", title=TITLE_B)], now=tuesday)

    assert count_dupes(conn, monday) == 0
    assert count_dupes(conn, tuesday) == 1


# ------------------------------------------------------------------ what "new" means


def test_unrendered_items_are_those_never_rendered(conn):
    insert_items(conn, [make_item(url="https://example.com/a", title="First")])
    assert len(unrendered_items(conn)) == 1

    stamp_digest_date(conn, ["https://example.com/a"], "2026-09-14")
    assert unrendered_items(conn) == []


def test_stamping_is_idempotent(conn):
    insert_items(conn, [make_item()])
    assert stamp_digest_date(conn, ["https://example.com/a"], "2026-09-14") == 1
    assert stamp_digest_date(conn, ["https://example.com/a"], "2026-09-15") == 0


def test_a_missed_run_catches_up_instead_of_losing_a_day(conn):
    """The reason "new" is `digest_date IS NULL` and not "first_seen_at is today".

    Monday runs and is rendered. Tuesday fetches but the render never happens -- laptop
    asleep, Action failed, away for the weekend. Wednesday must show Tuesday AND Wednesday.
    A date-based query would have lost Tuesday permanently.
    """
    monday = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    tuesday = monday + timedelta(days=1)
    wednesday = monday + timedelta(days=2)

    insert_items(conn, [make_item(url="https://example.com/mon", title="Monday story")], now=monday)
    stamp_digest_date(conn, ["https://example.com/mon"], "2026-09-14")

    # Tuesday: fetched, but no digest was produced.
    insert_items(
        conn, [make_item(url="https://example.com/tue", title="Tuesday story")], now=tuesday
    )
    insert_items(
        conn, [make_item(url="https://example.com/wed", title="Wednesday story")], now=wednesday
    )

    titles = [item.title for item in unrendered_items(conn)]
    assert titles == ["Tuesday story", "Wednesday story"]


def test_unrendered_items_are_ordered_oldest_first(conn):
    early = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
    for offset, name in enumerate(["c", "a", "b"]):
        insert_items(
            conn,
            [make_item(url=f"https://example.com/{name}", title=name)],
            now=early + timedelta(hours=offset),
        )
    assert [item.title for item in unrendered_items(conn)] == ["c", "a", "b"]


# --------------------------------------------------------------------------- source health


def test_failures_accumulate_and_only_one_counter_resets_on_recovery(conn):
    """The sequence this theme owes. Phase 1 could only ever report 0 or 1.

    `consecutive_failures` answers "is it failing right now" and correctly resets.
    `total_failures` and `last_failure_at` answer "has it been failing" and must not, or a
    source that fails every other day reads as perfectly healthy every time you look after a
    success -- the counter cleared and nothing else remembered.
    """
    failed_at = [datetime(2026, 9, 12 + day, 14, 0, tzinfo=UTC) for day in range(3)]
    for expected, when in enumerate(failed_at, start=1):
        record_source_health(conn, "hn", failed_at=when)
        (record,) = get_source_health(conn)
        assert record.consecutive_failures == expected
        assert record.total_failures == expected
        assert record.last_failure_at == when
        assert record.last_success_at is None

    success_at = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
    record_source_health(conn, "hn", succeeded_at=success_at)

    (record,) = get_source_health(conn)
    assert record.consecutive_failures == 0, "the right-now counter must reset"
    assert record.last_success_at == success_at
    assert record.total_failures == 3, "recovery must not erase that it failed three times"
    assert record.last_failure_at == failed_at[-1], "nor when it last failed"


def test_the_counters_are_computed_by_the_store_not_supplied(conn):
    """C1 from the layering review: the increment stays here because only here can see it.

    `SourceOutcome` carries no counts at all -- there is no field to pass through -- so the
    caller cannot compute them even by accident. Two failures recorded through the public
    API must produce 2, not 1 twice.
    """
    outcomes = [
        SourceOutcome(name="hn", failed_at=datetime(2026, 9, 12, 14, 0, tzinfo=UTC)),
        SourceOutcome(name="hn", failed_at=datetime(2026, 9, 13, 14, 0, tzinfo=UTC)),
    ]
    assert not hasattr(outcomes[0], "consecutive_failures")
    assert not hasattr(outcomes[0], "total_failures")

    for outcome in outcomes:
        record_source_health(
            conn, outcome.name, succeeded_at=outcome.succeeded_at, failed_at=outcome.failed_at
        )

    (record,) = get_source_health(conn)
    assert record.consecutive_failures == 2


def test_a_failed_run_cannot_be_recorded_as_a_success(conn):
    """The named break from the layering review, now unrepresentable.

    A retry wrapper or a future `run` command assembling a *complete* record would carry the
    previous `last_success_at` forward onto a failed run -- an obviously sensible thing to
    do, which under the old signature was read as success and reset the failure counter.

    Two things stop it. `SourceOutcome` refuses both timestamps at once, so "failed, but here
    is when it last worked" cannot be built. And the store preserves `last_success_at` across
    failures by itself, so there was never anything to carry.
    """
    success_at = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    record_source_health(conn, "hn", succeeded_at=success_at)

    with pytest.raises(ValidationError, match="exactly one"):
        SourceOutcome(
            name="hn",
            succeeded_at=success_at,
            failed_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        )
    with pytest.raises(StoreError, match="exactly one"):
        record_source_health(
            conn, "hn", succeeded_at=success_at, failed_at=datetime(2026, 9, 15, tzinfo=UTC)
        )
    with pytest.raises(StoreError, match="exactly one"):
        record_source_health(conn, "hn")

    record_source_health(conn, "hn", failed_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC))

    (record,) = get_source_health(conn)
    assert record.consecutive_failures == 1, "the failed run was recorded as a failure"
    assert record.last_success_at == success_at, "and the store kept when it last worked"


def test_health_records_are_per_source(conn):
    failed_at = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    record_source_health(conn, "hn", failed_at=failed_at)
    record_source_health(conn, "arxiv_cs_ai", failed_at=failed_at)
    record_source_health(conn, "hn", failed_at=failed_at)

    by_name = {record.name: record for record in get_source_health(conn)}
    assert by_name["hn"].consecutive_failures == 2
    assert by_name["arxiv_cs_ai"].consecutive_failures == 1


def test_health_timestamps_come_back_aware(conn):
    record_source_health(conn, "hn", succeeded_at=datetime(2026, 9, 14, 14, 0, tzinfo=UTC))
    record_source_health(conn, "hn", failed_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC))

    (record,) = get_source_health(conn)
    for stamp in (record.last_success_at, record.last_failure_at):
        assert stamp is not None
        assert stamp.tzinfo is not None


# ----------------------------------------------------------------------------- migration


def test_a_v1_database_upgrades_in_place(tmp_path):
    """The first schema change since phase 2, and the rows must survive it.

    Built from tests/fixtures/schema_v1.sql -- a frozen copy extracted from git -- and never
    from `init_db`. A migration test that constructs its "old" database by calling current
    code is migrating v2 to v2 within one release: it passes while proving nothing, which is
    the same defect as a snapshot test that certifies whatever it is handed.

    The alternative to migrating was "delete the file and refetch", which discards
    `first_seen_at` history and `raw_json` -- the things PLAN.md section 4 keeps so the
    ranker can be re-run over weeks of history offline, with 3b about to change the ranker.
    """
    path = tmp_path / "v1.db"
    old = sqlite3.connect(path, isolation_level=None)
    old.executescript(fixture_text("schema_v1.sql"))
    old.execute(
        "INSERT INTO items (url_hash, url, title, source, first_seen_at) "
        "VALUES ('abc', 'https://example.com/a', 'A thing', 'hn', '2026-09-01T00:00:00+00:00')"
    )
    old.execute(
        "INSERT INTO sources (name, last_success_at, consecutive_failures) "
        "VALUES ('hn', '2026-09-01T00:00:00+00:00', 2)"
    )
    assert old.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    old.close()

    conn = init_db(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 2

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)")}
        assert {"last_failure_at", "total_failures"} <= columns

        # Nothing lost, and the pre-existing counter is not reset by the upgrade.
        (item,) = conn.execute("SELECT url, title FROM items").fetchall()
        assert item["url"] == "https://example.com/a"

        (record,) = get_source_health(conn)
        assert record.name == "hn"
        assert record.consecutive_failures == 2
        assert record.total_failures == 0, "no history to back-fill; 0 is the honest answer"
        assert record.last_failure_at is None
    finally:
        conn.close()


def test_the_upgraded_database_then_behaves_like_a_fresh_one(tmp_path):
    """A migrated database must not be a second-class one."""
    path = tmp_path / "v1.db"
    old = sqlite3.connect(path, isolation_level=None)
    old.executescript(fixture_text("schema_v1.sql"))
    old.close()

    conn = init_db(path)
    try:
        record_source_health(conn, "hn", failed_at=datetime(2026, 9, 14, tzinfo=UTC))
        (record,) = get_source_health(conn)
        assert record.total_failures == 1
        assert record.last_failure_at == datetime(2026, 9, 14, tzinfo=UTC)
    finally:
        conn.close()


def test_migrating_is_idempotent(tmp_path):
    path = tmp_path / "v1.db"
    old = sqlite3.connect(path, isolation_level=None)
    old.executescript(fixture_text("schema_v1.sql"))
    old.close()

    init_db(path).close()
    conn = init_db(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"] == 1
    finally:
        conn.close()
