"""SQLite persistence. Reads and writes rows; no business logic.

Schema is PLAN.md section 4 with two deliberate deviations, both recorded here so a future
session does not "restore" them from the plan:

1. **`dupe_of TEXT` added to `items`.** Near-duplicates are stored and flagged, never
   dropped, so it is possible to see what got collapsed. It is a column rather than a key
   inside `raw_json` because phase 1 established that `raw` is the source's payload, verbatim
   and unedited -- writing our own marker into it would break that.

2. **`enabled` omitted from `sources`.** `sources.yaml` is the single source of truth for
   whether a source runs; this is the same reason `SourceHealth` has no `enabled` field. A
   column nothing reads is worse than an absent one, because eventually somebody reads it and
   then two places disagree about whether a source is on. docs/PLAN.md section 4 has been
   updated to match. If auto-disable-after-N-failures ever ships, it arrives as
   `auto_disabled_at` -- a name that cannot be confused with config intent.

Timestamps cross this boundary as ISO8601 UTC strings in both directions (CLAUDE.md), and
`datetime` objects are never handed to sqlite3: Python 3.12 deprecates the default adapter,
and relying on it would also silently drop tzinfo.
"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from digest.models import Item, SourceHealth

SCHEMA_VERSION = 2


class StoreError(Exception):
    """Raised for schema mismatches and unusable stored data."""


# ------------------------------------------------------------------------------ url canon

#: Query parameters stripped before hashing.
#:
#: The rule for anything added here: strip only parameters that are **unambiguously
#: tracking**, with no possible server-side meaning. Never a bare word an ordinary
#: application might use as a filter.
#:
#: This is not symmetric caution. A false *split* shows the same story twice and you shrug.
#: A false *collapse* drops an item via INSERT OR IGNORE -- it never enters the database,
#: logs nothing, and can only be noticed by missing something you did not know existed.
#:
#: `source` was in the original spec and was deliberately removed: `?source=trending` vs
#: `?source=likes` are different listings on plenty of sites, Hugging Face among them.
#: `ref` and `ref_src` are kept, but `ref` is the next most suspect -- suspect it first if a
#: collapse ever shows up that you cannot explain.
TRACKING_PARAMS = frozenset({"ref", "ref_src", "fbclid", "gclid", "igshid", "mc_cid"})
TRACKING_PREFIXES = ("utm_",)


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def canonical_url(url: str) -> str:
    """Normalise a URL so that trivially different spellings hash the same.

    Lowercases scheme and host, drops a leading `www.`, drops the fragment, drops tracking
    parameters, and strips a trailing slash. Pure function.
    """
    parts = urlsplit(url.strip())

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host.removeprefix("www.")
    netloc = f"{host}:{parts.port}" if parts.port else host

    path = parts.path.rstrip("/")

    params = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(key, value) for key, value in params if not _is_tracking(key)]

    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(kept), ""))


def url_hash(url: str) -> str:
    """sha256 hex of the canonical URL. The dedupe key."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


# -------------------------------------------------------------------- near-duplicate title

# Deliberately short. Every word removed here is a word two different stories can no longer
# be told apart by, so this stops at grammar and never touches subject matter.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "for", "to", "in", "on", "at", "by", "with",
    "from", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its", "this",
    "that", "these", "those", "s", "t", "via", "using", "how", "why", "what", "when",
})  # fmt: skip

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)

#: Dice, not Jaccard. At the 0.85 threshold Jaccard is effectively unreachable for real
#: headlines -- one extra word on a seven-token title already sits on the boundary and two
#: words is a miss -- and a threshold that never fires is worse than none, because it looks
#: like it is working.
NEAR_DUPLICATE_THRESHOLD = 0.85


def _tokens(title: str) -> set[str]:
    """Lowercased, punctuation-stripped, stopword-free token set."""
    cleaned = _NON_WORD.sub(" ", title.lower())
    return {token for token in cleaned.split() if token and token not in _STOPWORDS}


_HAS_DIGIT = re.compile(r"\d")


def _numeric_tokens(tokens: Iterable[str]) -> set[str]:
    """Tokens containing a digit anywhere -- not just all-digit ones.

    `7b` vs `8b`, `v2` vs `v3` and `gpt-5` vs `gpt-4` are exactly the distinctions that
    matter here, and none of them satisfy `str.isdigit()`.
    """
    return {token for token in tokens if _HAS_DIGIT.search(token)}


def title_similarity(left: str, right: str) -> float:
    """Dice coefficient over token sets: 2|A n B| / (|A| + |B|)."""
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def titles_are_near_duplicates(left: str, right: str) -> bool:
    """Dice >= threshold AND identical numeric tokens.

    The numeric guard is not decoration. This digest's subject matter is version numbers and
    model names, where Dice alone has a false-positive class that lands squarely on target:
    "RTX 5090 Super at CES 2027" vs "RTX 5080 Super at CES 2027" scores 0.875 and would
    collapse two different product announcements. Same for Claude Opus 4.5 vs 4.6, or two
    arXiv papers whose titles differ by a number. If the digits differ at all, it is not a
    duplicate however high the ratio.

    Known limitation, out of scope: inflectional variants are different tokens, so
    "Anthropic releases X" vs "X released by Anthropic" scores 0.80 and will not match.
    Fixing that needs stemming.
    """
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if _numeric_tokens(left_tokens) != _numeric_tokens(right_tokens):
        return False
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    return 2 * overlap / (len(left_tokens) + len(right_tokens)) >= NEAR_DUPLICATE_THRESHOLD


# ------------------------------------------------------------------------------ timestamps


def to_iso(value: datetime) -> str:
    """Aware datetime -> ISO8601 UTC string. Refuses naive input."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise StoreError(f"refusing to store naive datetime {value!r}; convert it first")
    return value.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    """ISO8601 string -> aware datetime.

    Raises rather than assuming UTC on a naive value. Everything we write goes through
    `to_iso`, so a naive string means the row was written by something else or corrupted --
    and the tempting fix, loosening the model validator, would undo phase 0.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StoreError(f"stored timestamp {value!r} is not ISO8601: {exc}") from exc
    if parsed.tzinfo is None:
        raise StoreError(
            f"stored timestamp {value!r} has no timezone. Everything written by store.to_iso "
            f"carries one, so this row came from somewhere else. Fix the data, do not relax "
            f"the Item validator."
        )
    return parsed


# ---------------------------------------------------------------------------------- schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id             INTEGER PRIMARY KEY,
  url_hash       TEXT UNIQUE NOT NULL,
  url            TEXT NOT NULL,
  title          TEXT NOT NULL,
  source         TEXT NOT NULL,
  topic          TEXT,
  author         TEXT,
  published_at   TEXT,
  first_seen_at  TEXT NOT NULL,
  raw_json       TEXT,
  score          REAL,
  score_reason   TEXT,
  summary        TEXT,
  digest_date    TEXT,
  feedback       INTEGER DEFAULT 0,
  dupe_of        TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_digest ON items(digest_date);
CREATE INDEX IF NOT EXISTS idx_items_seen   ON items(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_items_dupe   ON items(dupe_of);

CREATE TABLE IF NOT EXISTS sources (
  name                 TEXT PRIMARY KEY,
  kind                 TEXT,
  url                  TEXT,
  etag                 TEXT,
  last_modified        TEXT,
  last_success_at      TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_failure_at      TEXT,
  total_failures       INTEGER NOT NULL DEFAULT 0
);
"""

#: Upgrades keyed by the version being upgraded *from*. Applied in order until the stored
#: version reaches SCHEMA_VERSION.
#:
#: Written rather than skipped because the alternative -- "delete the file and refetch" --
#: discards accumulated `first_seen_at` history and `raw_json`, which PLAN.md section 4 says
#: exist precisely so the ranker can be re-run over weeks of history offline. Phase 3b is
#: about to change the ranker. And once phase 5's Action commits digest.db back to the repo,
#: the database stops being disposable and the first migration would get written under
#: pressure instead of while the data is still cheap.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "ALTER TABLE sources ADD COLUMN last_failure_at TEXT",
        "ALTER TABLE sources ADD COLUMN total_failures INTEGER NOT NULL DEFAULT 0",
    ),
}

_ITEM_COLUMNS = (
    "url",
    "title",
    "source",
    "topic",
    "author",
    "published_at",
    "raw_json",
    "score",
    "score_reason",
    "summary",
)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode with WAL enabled.

    ⚠️ NOTE FOR PHASE 5 (GitHub Action delivery): WAL means recent transactions can still be
    in the `digest.db-wal` sidecar rather than the `.db` file itself. Committing the `.db`
    alone from CI produces a database that is silently *behind* -- it opens cleanly, looks
    fine, and is missing the last run. Call `checkpoint()` before the commit step, or commit
    the sidecars too. Nothing in the health footer can catch this, because the run succeeded.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the WAL back into the main database file and truncate it.

    Call before anything copies, commits or backs up the `.db` file. See `connect`.
    """
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def init_db(path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    """Create the schema if absent and verify the version. Idempotent.

    `create=False` refuses to bring a database into existence. Readers want it: `sqlite3`
    creates an empty file for any path you hand it, so `digest render --db typo.db` used to
    produce a brand-new database and the message "Nothing new" -- a confident empty digest
    indistinguishable from a real one. The failure and the success looked the same.
    """
    if not create and not Path(path).exists():
        raise StoreError(
            f"no digest database at {Path(path).resolve()}\n"
            f"       This command reads an existing database and will not create one.\n"
            f"       Run `digest fetch` first, or pass --db with the right path."
        )
    conn = connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")

    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        found = row["version"]
        if found > SCHEMA_VERSION:
            conn.close()
            raise StoreError(
                f"database at {path} is schema version {found}, but this code understands "
                f"version {SCHEMA_VERSION}. Refusing to touch it -- a newer schema written by "
                f"older code corrupts data silently. Upgrade the code, or point --db elsewhere."
            )
        if found < SCHEMA_VERSION:
            _migrate(conn, found, path)

    conn.executescript(_SCHEMA)
    return conn


def _migrate(conn: sqlite3.Connection, from_version: int, path: str | Path) -> None:
    """Upgrade an older database in place, all steps or none.

    Wrapped in an explicit transaction because `connect` opens in autocommit
    (`isolation_level=None`): without it a failure half-way leaves a database whose
    `schema_version` disagrees with its columns, which is exactly the silent corruption the
    version check exists to prevent. SQLite makes DDL transactional, so the rollback is real.
    """
    version = from_version
    conn.execute("BEGIN")
    try:
        while version < SCHEMA_VERSION:
            steps = _MIGRATIONS.get(version)
            if steps is None:
                raise StoreError(
                    f"database at {path} is schema version {version} and no migration to "
                    f"{version + 1} exists. Add one to _MIGRATIONS, or delete the file to "
                    f"start fresh -- deleting discards stored history."
                )
            for statement in steps:
                conn.execute(statement)
            version += 1
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ----------------------------------------------------------------------------------- items


def is_near_duplicate(
    conn: sqlite3.Connection,
    item: Item,
    window_days: int = 3,
    now: datetime | None = None,
) -> str | None:
    """Return the url_hash of a recent item with a near-identical title, if any.

    The window is measured against `first_seen_at`, not `published_at`: the latter is
    nullable, and an item with no publication date would otherwise escape the check
    entirely.

    Rows already flagged as duplicates are excluded, so a third copy of the same story points
    at the original rather than forming a chain.
    """
    cutoff = to_iso((now or datetime.now(tz=UTC)) - timedelta(days=window_days))
    own_hash = url_hash(item.url)

    rows = conn.execute(
        "SELECT url_hash, title FROM items "
        "WHERE first_seen_at >= ? AND dupe_of IS NULL AND url_hash != ? "
        "ORDER BY first_seen_at",
        (cutoff, own_hash),
    ).fetchall()

    for row in rows:
        if titles_are_near_duplicates(item.title, row["title"]):
            return str(row["url_hash"])
    return None


def insert_items(
    conn: sqlite3.Connection,
    items: Sequence[Item],
    now: datetime | None = None,
    window_days: int = 3,
) -> int:
    """Insert items that are not already stored. Returns the count of rows actually inserted.

    Named `insert_items` and not `upsert_items`, which is what it was called while being
    `INSERT OR IGNORE`: an upsert *updates* the existing row, and this discards the incoming
    one. The name promised the opposite of the behaviour, and the payload was timed for phase
    3b -- `score`, `topic` and `summary` are already parameters here, so the obvious call for
    writing a ranker's output back would have returned 0 and written nothing, indistinguishable
    from a normal second run.

    Not `insert_new_items` either: "new" already means "not yet rendered" three lines away in
    the CLI's output, and reusing it here would reintroduce the collision one rename removed.

    The write-back path phase 3b needs is deliberately NOT here. That is 3b's design decision.

    Every item in the batch gets the *same* `first_seen_at`, so "this run" is exactly
    queryable afterwards -- which is what `count_dupes` relies on, and what makes the
    missed-run catch-up test unambiguous.
    """
    run_started_at = to_iso(now or datetime.now(tz=UTC))
    before = conn.total_changes

    for item in items:
        dupe_of = is_near_duplicate(conn, item, window_days=window_days, now=now)
        conn.execute(
            "INSERT OR IGNORE INTO items "
            "(url_hash, url, title, source, topic, author, published_at, first_seen_at, "
            " raw_json, score, score_reason, summary, dupe_of) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                url_hash(item.url),
                item.url,
                item.title,
                item.source,
                item.topic,
                item.author,
                to_iso(item.published_at) if item.published_at else None,
                run_started_at,
                # The source's payload, verbatim -- not the serialised Item, whose other
                # fields are already columns. PLAN.md section 4: this is what lets phase 4
                # re-score weeks of history after the ranker changes.
                json.dumps(item.raw, ensure_ascii=False),
                item.score,
                item.score_reason,
                item.summary,
                dupe_of,
            ),
        )

    return conn.total_changes - before


def count_dupes(
    conn: sqlite3.Connection, run_started_at: datetime, source: str | None = None
) -> int:
    """How many rows written in this run were flagged as near-duplicates."""
    sql = "SELECT COUNT(*) AS n FROM items WHERE first_seen_at = ? AND dupe_of IS NOT NULL"
    params: list[Any] = [to_iso(run_started_at)]
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    return int(conn.execute(sql, params).fetchone()["n"])


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        url=row["url"],
        title=row["title"],
        source=row["source"],
        author=row["author"],
        published_at=from_iso(row["published_at"]) if row["published_at"] else None,
        raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
        topic=row["topic"],
        score=row["score"],
        score_reason=row["score_reason"],
        summary=row["summary"],
    )


def unrendered_items(conn: sqlite3.Connection) -> list[Item]:
    """Items that have never appeared in a digest, oldest first.

    Named for what it means rather than "new", which meant two different things in
    adjacent user-visible output: the run log's count of rows *inserted this run*, and
    this, *not yet rendered*. Run `digest fetch` twice then `digest render` and the two
    numbers disagree completely while both are correct.

    "New" is `digest_date IS NULL` -- deliberately NOT "first_seen_at is today". Two reasons,
    both of which will happen:

    * `first_seen_at` is UTC and the digest day is America/Vancouver. A 07:00 local run is
      14:00 UTC, so a calendar-date comparison splits one morning's items across two UTC days.
    * A missed run -- laptop asleep, Action failed, away for the weekend -- would lose those
      days permanently under a date query. With this, the next run catches up by itself.
    """
    rows = conn.execute(
        # Interpolation is safe here: _ITEM_COLUMNS is a fixed module-level tuple, never
        # user input.
        f"SELECT {', '.join(_ITEM_COLUMNS)} FROM items "
        "WHERE digest_date IS NULL ORDER BY first_seen_at, id"
    ).fetchall()
    return [_row_to_item(row) for row in rows]


def stamp_digest_date(
    conn: sqlite3.Connection, urls: Iterable[str], digest_date: date | str
) -> int:
    """Mark items as having appeared in a digest. Wired to `render` in phase 3c.

    Until then this is exercised only by tests -- it is what makes `new_items` shrink, so the
    catch-up behaviour cannot be verified without it.
    """
    stamp = digest_date.isoformat() if isinstance(digest_date, date) else digest_date
    hashes = [(stamp, url_hash(url)) for url in urls]
    before = conn.total_changes
    conn.executemany(
        "UPDATE items SET digest_date = ? WHERE url_hash = ? AND digest_date IS NULL", hashes
    )
    return conn.total_changes - before


# --------------------------------------------------------------------------- source health


def record_source_health(
    conn: sqlite3.Connection,
    name: str,
    *,
    succeeded_at: datetime | None = None,
    failed_at: datetime | None = None,
) -> None:
    """Record one run's outcome for one source. Exactly one timestamp, never a flag.

    Takes the outcome rather than deducing it. The previous signature accepted a
    `SourceHealth` and recovered "did this run succeed" from `last_success_at is not None` --
    correct only by accident of how one caller built the object, and silently wrong for any
    caller that filled the field in to make the record complete.

    A timestamp rather than a bool because the store needs to know *when*: `last_failure_at`
    is half of what makes a recovered source's history survive, and a bool could not supply
    it. Taking `now()` here instead would put a hidden clock in the store, days after the
    project moved clocks to injection.

    **The counters stay here, deliberately.** `consecutive_failures` and `total_failures` are
    read-modify-write against the stored row, which is persistence, not business logic -- and
    the caller structurally cannot do it, because it cannot see the previous value. Phase 1's
    fetch loop could only ever report 0 or 1 for exactly that reason. `SourceOutcome` carries
    no counts at all so there is nothing to pass through and nothing to tempt anyone into
    computing caller-side.

    A success updates `last_success_at` and clears `consecutive_failures`. It does **not**
    clear `last_failure_at` or `total_failures`: those are the record that survives recovery,
    without which a source failing every other day reads as healthy every time you look.
    """
    if (succeeded_at is None) == (failed_at is None):
        both = succeeded_at is not None
        raise StoreError(
            f"record_source_health({name!r}): pass exactly one of succeeded_at / failed_at, "
            f"got {'both' if both else 'neither'}. A run either succeeded or failed."
        )

    if succeeded_at is not None:
        conn.execute(
            "INSERT INTO sources (name, last_success_at, consecutive_failures) "
            "VALUES (?, ?, 0) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  last_success_at = excluded.last_success_at, consecutive_failures = 0",
            (name, to_iso(succeeded_at)),
        )
    else:
        conn.execute(
            "INSERT INTO sources "
            "(name, last_success_at, consecutive_failures, last_failure_at, total_failures) "
            "VALUES (?, NULL, 1, ?, 1) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  consecutive_failures = sources.consecutive_failures + 1,"
            "  total_failures = sources.total_failures + 1,"
            "  last_failure_at = excluded.last_failure_at",
            (name, to_iso(failed_at)),
        )


def get_source_health(conn: sqlite3.Connection) -> list[SourceHealth]:
    """Every source's persisted health record, by name."""
    rows = conn.execute(
        "SELECT name, last_success_at, consecutive_failures, last_failure_at, total_failures "
        "FROM sources ORDER BY name"
    ).fetchall()
    return [
        SourceHealth(
            name=row["name"],
            last_success_at=from_iso(row["last_success_at"]) if row["last_success_at"] else None,
            consecutive_failures=row["consecutive_failures"],
            last_failure_at=from_iso(row["last_failure_at"]) if row["last_failure_at"] else None,
            total_failures=row["total_failures"],
        )
        for row in rows
    ]
