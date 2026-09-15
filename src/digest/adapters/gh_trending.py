"""GitHub Trending, by scraping. There is no API and there never has been.

This is the most fragile adapter in the project and the selectors below are why: they are
Primer **CSS utility classes**, not semantic anchors, so they change when GitHub restyles
something -- no deprecation, no contract, no notice. Every one is a module-level constant so
a layout change is a one-line fix rather than an archaeology session.

The important design choice is what happens when they stop matching: this **raises**. An
empty list would be indistinguishable from a quiet day on the trending page (which does not
happen -- it is always ~20 repos), and a source that silently returns nothing is precisely
the rot the health footer cannot catch. See CLAUDE.md, the zero-items rule.
"""

import re

import httpx
from selectolax.parser import HTMLParser, Node

from digest.adapters._mapping import normalise_title
from digest.adapters.base import Adapter
from digest.config import Source
from digest.errors import SourceBlockedError, SourcePayloadError
from digest.models import Item

# --- Selectors. Verified 2026-09-14. Change here, not inline. ----------------------------
SELECTOR_ROW = "article.Box-row"
SELECTOR_REPO_LINK = "h2 a"
SELECTOR_DESCRIPTION = "p"
SELECTOR_LANGUAGE = '[itemprop="programmingLanguage"]'
SELECTOR_STARS_TODAY = "span.d-inline-block.float-sm-right"
# -----------------------------------------------------------------------------------------

GITHUB_BASE = "https://github.com"

_STARS_TODAY = re.compile(r"([\d,]+)\s+stars?\s+today")


class GitHubTrendingError(SourcePayloadError):
    """The page no longer looks like GitHub Trending.

    Kept as a name rather than folded away: it is the one adapter error anyone has ever
    grepped for. It now subclasses the shared hierarchy, so `isinstance(exc, AdapterError)`
    classifies it alongside the failures the other four adapters raise -- which were bare
    RuntimeError and ValueError, ungreppable and indistinguishable from a crash.
    """


class GhTrendingAdapter(Adapter):
    kind = "gh_trending"

    async def fetch(self, client: httpx.AsyncClient, source: Source) -> list[Item]:
        if source.url is None:  # pragma: no cover -- the config validator forbids it
            raise ValueError(f"source {source.name!r} has no url")

        response = await client.get(source.url)
        if response.status_code == 403:
            # GitHub serves a 403 with an HTML body to clients it dislikes. That body parses
            # perfectly well and yields zero rows, so without this check a block would look
            # exactly like a quiet day.
            raise SourceBlockedError(
                f"{source.name}: {source.url} returned 403 -- GitHub is blocking this "
                f"client. Check the User-Agent (DIGEST_USER_AGENT) and back off; do not "
                f"retry in a tight loop."
            )
        response.raise_for_status()

        items = parse_trending(response.text, source.name, source.url)
        return items


def parse_trending(html: str, source_name: str, url: str) -> list[Item]:
    """Parse the trending page. Raises if it no longer looks like the trending page."""
    tree = HTMLParser(html)
    rows = tree.css(SELECTOR_ROW)

    if not rows:
        raise GitHubTrendingError(
            f"{source_name}: selector {SELECTOR_ROW!r} matched nothing on {url}. The page "
            f"is always ~20 repositories, so zero means the layout changed (or the body is "
            f"an error page), not that today was quiet. Fix SELECTOR_ROW in "
            f"adapters/gh_trending.py and re-record the fixture with "
            f"`uv run python scripts/record_fixtures.py --source gh_trending`."
        )

    items = [item for row in rows if (item := _item_from_row(row, source_name)) is not None]

    if not items:
        raise GitHubTrendingError(
            f"{source_name}: found {len(rows)} rows but mapped none of them. "
            f"{SELECTOR_REPO_LINK!r} probably no longer selects the repo link. Same fix as "
            f"above, in adapters/gh_trending.py."
        )
    return items


def _item_from_row(row: Node, source_name: str) -> Item | None:
    link = row.css_first(SELECTOR_REPO_LINK)
    if link is None:
        return None
    href = (link.attributes.get("href") or "").strip()
    if not href.startswith("/"):
        return None

    full_name = href.lstrip("/")
    owner, _, _repo = full_name.partition("/")
    description = _text(row.css_first(SELECTOR_DESCRIPTION))

    return Item(
        url=f"{GITHUB_BASE}/{full_name}",
        # `owner/repo` alone is close to useless in a digest and gives the keyword ranker
        # nothing to match on, so the one-line description is part of the title. These are
        # short taglines, not article bodies -- this is not the thing CLAUDE.md forbids.
        title=f"{full_name}: {description}" if description else full_name,
        source=source_name,
        author=owner or None,
        # The trending page carries no date. Nullable by design rather than faked with
        # "now", which would make every repo look freshly published to the recency bonus.
        published_at=None,
        # Synthesised rather than a source payload -- there is no JSON here to keep verbatim,
        # so this is the closest thing to one. It carries only what the page said.
        # Deliberately NOT a `scraped_at` timestamp: `first_seen_at` is the column that owns
        # "when we saw this" (PLAN.md section 4), and putting our own clock inside `raw` both
        # duplicated it and made the row non-reproducible from a fixed fixture.
        raw={
            "full_name": full_name,
            "description": description,
            "language": _text(row.css_first(SELECTOR_LANGUAGE)) or None,
            "stars_today": _stars_today(row),
        },
    )


def _text(node: Node | None) -> str:
    if node is None:
        return ""
    return normalise_title(node.text(strip=True))


def _stars_today(row: Node) -> int | None:
    """`'2,233 stars today'` -> `2233`. None when the row does not show one."""
    for node in row.css(SELECTOR_STARS_TODAY):
        match = _STARS_TODAY.search(_text(node))
        if match:
            return int(match.group(1).replace(",", ""))
    return None
