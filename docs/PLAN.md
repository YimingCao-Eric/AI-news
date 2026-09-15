# 05 · Personalized Daily News Digest — Build Plan

Written 2026-09-12. Companion to `05-personalized-daily-news-digest.md` (the idea outline) and
`interests.md` (your topic/source notes). Sequencing assumptions come from `../PROJECT_TIMING_PLAN.md`:
this is the **small side project for weeks 1–4** of LLM Engineering, finishing around Oct 5–11.
Empty repo `..\..\AI-news` already exists — use it.

---

## 0. Scope and definition of done

**What it is:** one scheduled job that fetches from a fixed source list, drops anything seen before,
ranks what's left against your interest profile, summarises the top slice, and produces one page you
read each morning.

**What it is not (v1):** not a web app, not multi-user, not real-time, no ML training, no
recommendation model. Those are v2 questions and most of them will turn out not to matter.

**Done when** — all five true for seven consecutive days:

1. Runs unattended on a schedule; you don't type a command to get it.
2. Takes under 3 minutes wall-clock and under CAD $0.10/day in API spend.
3. You actually read it before reading Hacker News directly.
4. Fewer than 3 items per day make you think "why is this here".
5. A source going down degrades one section, never the whole digest.

Criterion 3 is the real one. The others are proxies.

---

## 1. Interest profile

### 1.1 What you wrote (`interests.md`) — kept as the spine

Topics: LLM (latest updates, new models) · Agentic AI (coding agents, SOTA agent design,
commercially successful / high-usage agents) · Electronic products (phones, computers, laptops, smart
glasses, headphones, CPUs, GPUs, consoles) · Video games.

Sources: GitHub · Hugging Face · official websites · events (BlizzCon, Gamescom, Apple Event…).

That's a good list. Two structural gaps: it has no *negative* filter (what you don't want), and the
four topics have very different natural volumes — LLM/agents produce hundreds of items a day, events
produce a handful a month. The digest needs per-topic quotas or the game and hardware sections will
be crushed by AI noise. See §5.

### 1.2 Proposed additions

Drawn from what you've told me about your work and what you already spend time on. Adopt or cut each
one deliberately — every topic you add costs signal in the other sections.

| Candidate topic | Why it fits you | Keep? |
|---|---|---|
| **AI coding agent releases specifically** (Claude Code, Codex, Cursor, Copilot, Aider, OpenCode) | You build your products with Claude Code; a changelog entry here changes how you work that week | Strong yes — arguably its own section, not a sub-topic of "agentic AI" |
| **MCP servers and the MCP ecosystem** | Agents course week 6 is MCP; idea 03's tool router depends on it | Yes |
| **Local / self-hosted inference** (Ollama, llama.cpp, vLLM, quantisation, GGUF releases) | You run Ollama on the RTX 5070 laptop and are building a k3s home cluster | Yes |
| **Consumer GPU / VRAM news framed for local inference** | Your hardware interest is not generic benchmarking, it's "what can I run at home" | Yes — but as a *lens* on the hardware section, not a separate feed |
| **Smart glasses with prescription support** | Your hard filter; a general "smart glasses" feed will mostly be noise about over-glasses models | Yes, as a keyword rule rather than a source |
| **RAG / retrieval and embedding tooling** | NextTier RAG work, idea 04, course week 5 | Yes, low quota |
| **Agent evaluation & observability** (LangSmith, LangFuse, evals, LLM-as-judge) | The part of agent building that's hardest to self-teach and where tooling moves fast | Yes, low quota |
| **Homelab / k3s / GPU orchestration** | The three-laptop + Mac Mini cluster plan | Optional, low quota |
| **CRPG / open-world RPG news** (not gaming in general) | Witcher/Cyberpunk/BG3 taste — "video games" unfiltered will hand you mobile and esports news | Yes — narrow "video games" to this |
| **Game-engine and game-AI tech** (NPC LLMs, procedural narrative) | Direct input to ideas 01 and 02 | Yes, low quota — this is the bridge between your hobby and your build queue |
| **Speech / ASR / diarisation models** | Idea 03 is built on MOSS-Transcribe-Diarize; you want to hear when something better lands | Yes, low quota |
| **Anthropic / OpenAI / Google pricing and rate-limit changes** | Directly changes your per-session cost maths on ideas 01 and 04 | Yes — high value, low volume |

**Explicit anti-topics** (filter these out; the single biggest quality lever):
funding rounds and valuations, "X company announces partnership", AI-doom and AI-hype opinion essays,
crypto, mobile-game and gacha news, esports results, phone-case/accessory reviews, listicles
("10 best…"), anything whose title is a question ("Is AGI here?"), press releases with no artefact
you can click through to (no repo, no model, no changelog, no product page).

### 1.3 Proposed `interests.md` v2

A machine-readable version is written next to this plan as **`interests.proposed.md`** — same content
as your file plus the above, in a YAML shape the ranker can consume directly (topic, weight, keywords,
anti-keywords, daily quota). Your original `interests.md` is untouched; merge when you agree.

---

## 2. Source catalogue

Verified 2026-09-12 unless marked. "Verify" means I confirmed the endpoint returns the expected
content; unmarked rows are standard, well-known feed URLs you should HEAD-check on day one.

**This section is a log of observations, so every number in it carries the date it was taken.**
A count without a date reads as a constant, and then a genuinely quiet Sunday looks like a
broken adapter — which is the mistake the arXiv row below caused once already. Volumes vary by
weekday, holiday and category; when you re-measure, add a line rather than replacing one.

### Tier 1 — build against these first (5 sources, one per topic)

| Source | Endpoint | Notes |
|---|---|---|
| Hacker News front page | `https://hn.algolia.com/api/v1/search?tags=front_page` | ✅ verified. JSON, no key, no scraping. Fields: `title`, `url`, `points`, `num_comments`, `created_at`, `objectID`. Use `search_by_date` with `tags=story&numericFilters=points>100` for a cleaner daily cut |
| GitHub Trending | Scrape `https://github.com/trending?since=daily&spoken_language_code=en` — or the per-language variants | No official API, never has been. The HTML is stable; a 30-line parse with **selectolax** (corrected 2026-09-15 from "BeautifulSoup" — CLAUDE.md fixes the stack on selectolax and forbids adding a dependency, so this line would have had you install one). Alternatives if you'd rather not scrape: `mshibanami/GitHubTrendingRSS` (prebuilt RSS per language), `vitalets/github-trending-repos` (GitHub issue notifications) |
| Hugging Face daily papers | `https://huggingface.co/api/daily_papers` | ✅ verified. JSON; per item `paper.{id,title,summary,authors}`, `publishedAt`, `numComments`, `submittedBy`, `organization`. This is the curated feed — far better signal than raw new-model listings |
| AI company blogs (bundle) | Official feeds first; `https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_<name>.xml` where none exists | ✅ verified. **Freshness checked 2026-09-14** — the instruction below is discharged, see the log. Repo regenerates hourly via Actions; feed quality is per-feed, not per-mirror. Saves you writing five scrapers. Live list is `sources.yaml` |
| arXiv cs.AI | `https://rss.arxiv.org/rss/cs.AI` | ✅ verified. Also `cs.CL` (NLP), `cs.MA` (multi-agent) — all three are subscribed. Volume is much higher than first measured; see the log. Quota this hard or filter by keyword before it reaches the ranker |

That's enough to build the whole pipeline end to end. **Do not add source six until phase 3 works.**

### Volume and freshness observed — a log, not a table of constants

**2026-09-12** (first look, cs.AI only): arXiv cs.AI RSS returned **50 items**, same-day.

**2026-09-14** (`scripts/record_fixtures.py --source all`, the recording the pipeline snapshot
is still built from — `tests/fixtures/manifest.json` carries the instant):

| Source | Observed | Reaching the store |
|---|---|---|
| arXiv | cs.AI **270** entries, cs.CL **115**, cs.MA **16** | 209 after the announce filter |
| `ai_blogs` | 6 feeds, 1824 entries total (openai_news 1193, anthropic_news 257, claude 237, deepmind 100, cursor 22, anthropic_research 15) | 112 after the 30-day ingest cutoff |
| HF daily papers | **50** papers | 50 |
| HN (`points>100`, 48h) | 30 hits returned, 55 matching | 30 (`fetch_limit`) |
| GitHub Trending | **24** rows | 24 |

**cs.AI at 270 is 5.4× the 50 recorded two days earlier, and that is the point of dating
these.** arXiv volume genuinely swings — weekday against weekend, holidays, and per category
(cs.MA is 16 on the same morning cs.AI is 270). Roughly 38% of that 270 was `replace` /
`replace-cross`, i.e. v2 revisions rather than new work, which the adapter filters out; so
"how many entries arrived" and "how many are new work" are two different numbers and the
gap moves too. **Do not treat any of these as a threshold.** If you see 180 cs.AI entries on
a Sunday, that is a Sunday, not a fault — the thing that would indicate a fault is
`0 new/cross of 270`, which the adapter reports with its denominator for exactly this reason.

**Freshness, same date** — this is what discharged the "check freshness" instruction above,
and the finding was not the one expected: the Olshansk mirror is not uniformly stale, it is
stale **per feed**. Both official feeds were fresh; three of four mirror feeds were months
old *while returning HTTP 200 with well-formed XML*. Dropped that morning:
`anthropic_engineering` (112d), `meta_ai` (49d), `mistral` (115d); every alternative checked
returned 404, so the replacements come from the same mirror. Hence per-feed
`stale_after_days` in `sources.yaml` rather than one global number, and hence the STALE line
in the run summary: these feeds do not fail, they go quiet, and nothing else would say so.

### Tier 2 — add once the pipeline is proven

| Topic | Source | Endpoint / method |
|---|---|---|
| LLM/official | OpenAI news | `https://openai.com/news/rss.xml`, research `https://openai.com/blog/rss.xml` |
| LLM/official | Google DeepMind | `https://deepmind.google/blog/rss.xml` |
| LLM/official | Anthropic (direct) | No official RSS as of now — hence the mirror above. Community mirrors: `taobojlen/anthropic-rss-feed`, RSSHub route, `Olshansk/rss-feeds` |
| Tooling releases | GitHub Releases Atom — the single highest-signal-per-byte source you have | `https://github.com/<owner>/<repo>/releases.atom`. Start with: `anthropics/claude-code`, `ollama/ollama`, `ggml-org/llama.cpp`, `vllm-project/vllm`, `langchain-ai/langgraph`, `openai/openai-agents-python`, `modelcontextprotocol/servers`, `run-llama/llama_index`, `huggingface/transformers`, `astral-sh/uv`, `OpenMOSS/MOSS-Transcribe-Diarize` |
| Models | HF new/trending models | `https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=30`. ⚠️ Do **not** sort by `createdAt` — thousands of junk uploads per day. Filter `downloads`/`likes` thresholds. Third-party prebuilt feed: `zernel/huggingface-trending-feed` |
| Anything without a feed | RSSHub | Self-hostable "everything is RSSible" gateway; has routes for HF daily papers, GitHub trending, Steam, Apple newsroom and hundreds more. Running your own instance in Docker is the general escape hatch for your "official website" and "event" source categories |
| Hardware | Notebookcheck / Tom's Hardware / AnandTech-successors / VideoCardz | All publish RSS; pick two, not five. VideoCardz is the best signal for GPU/CPU leaks and launches |
| Hardware/official | Apple Newsroom | `https://www.apple.com/newsroom/rss-feed.rss` |
| Games | Steam news per app | `https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=<id>&count=5` — no key needed for this interface. Watch the appids of games you actually play; this is how you catch CDPR/Larian patch notes without a gaming-news site |
| Games | RPS / PC Gamer / Eurogamer RSS | Standard `/feed` endpoints. Expect to keyword-filter hard — see anti-topics |
| Games/events | Event calendars (Gamescom, Apple Event, BlizzCon, CES, WWDC, GDC) | No feed exists for "an event is happening". Hand-maintain a small YAML of dates and have the digest surface a countdown line the week before. Ten minutes of work, solves the whole "events" category |
| Community | Reddit as RSS | `https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day` — also `r/singularity` (skip), `r/patientgamers`, `r/cyberpunkgame`. Needs a real User-Agent or you get 429 |
| Community | Lobsters | `https://lobste.rs/t/ai.rss` — much lower volume than HN, higher hit rate for tooling |

### 2.1 Two rules that will save you a day each

**Prefer an API or feed over scraping, always.** Every scraper you write is a thing that breaks
silently in six weeks. Ranked preference: official JSON API → official RSS/Atom → community-maintained
mirror feed → RSSHub instance → your own scraper. GitHub Trending is the only tier-1 source where you
land on "own scraper", and even there a mirror exists.

**Every source gets a health record.** Store `last_success_at` and `consecutive_failures` per source
and print a one-line source-health footer in every digest. A dead feed that fails silently is how
these projects rot — you stop seeing a topic and never notice it stopped rather than went quiet.

---

## 3. Architecture

Five stages, each a pure function where possible, each independently runnable from the CLI.

```
fetch → normalise → dedupe/store → rank → summarise → render → deliver
```

- **fetch**: per-source adapter, returns raw payloads. Only this layer touches the network.
  Conditional GET (`If-None-Match` / `If-Modified-Since`) on every RSS source; cache the ETag.
  **Amended 2026-09-15 — not implemented, and deliberately so.** `ai_blogs` builds the headers
  and handles 304 (both tested); nothing stores the validators, so every request goes out
  unconditional, and `arxiv` does not do it at all. Caching the ETag needs a `feed_state`
  table: the `sources` row has one `etag` column and `ai_blogs` has six feeds, which is the
  detail this line did not foresee. The payoff is politeness, not speed, on six small files
  fetched once a day. Trigger: a host rate-limiting us. Tracked as DD-7.
- **normalise**: maps each source's shape into one `Item`. Every adapter's only job.
- **dedupe/store**: canonicalise the URL (strip `utm_*`, trailing slash, `?ref=`), hash it, insert
  with `INSERT OR IGNORE`. Also catch the same story from three outlets: near-duplicate title match
  (token-set ratio ≥ 0.85) within a 3-day window.
- **rank**: score against the interest profile. Phase 3 = keyword rules; phase 4 = LLM; phase 6
  (optional) = embeddings.
- **summarise**: LLM, top N only.
- **render**: Markdown first; HTML later if you want it pretty.
- **deliver**: file → then push/email.

**Language/stack:** Python 3.12, `httpx` (async), `feedparser`, `selectolax` *(chosen over
`beautifulsoup4` in phase 0; CLAUDE.md now fixes the stack and forbids substitutions)*,
`pydantic` v2 for `Item`, SQLite via stdlib `sqlite3` (do **not** reach for SQLAlchemy here — the JHA
async-SQLAlchemy/asyncpg pain you already hit is not worth re-importing for a single-table script),
`jinja2` for rendering, `uv` for deps. One process, no queue, no server.

**Why SQLite and not Postgres:** you want this to run from a laptop, a cron job, and possibly a GitHub
Action with no infrastructure. If you later merge ingestion with idea 04, that's the moment to
promote the store to Postgres — and the adapter layer is what makes that a one-day change.

---

## 4. Data model

```sql
CREATE TABLE items (
  id             INTEGER PRIMARY KEY,
  url_hash       TEXT UNIQUE NOT NULL,   -- sha256 of canonical url
  url            TEXT NOT NULL,
  title          TEXT NOT NULL,
  source         TEXT NOT NULL,          -- 'hn' | 'gh_trending' | 'hf_papers' | ...
  topic          TEXT,                   -- assigned at rank time
  author         TEXT,
  published_at   TEXT,                   -- ISO8601, source's time
  first_seen_at  TEXT NOT NULL,          -- ISO8601, ours
  raw_json       TEXT,                   -- the original payload, for reprocessing
  score          REAL,
  score_reason   TEXT,
  summary        TEXT,
  digest_date    TEXT,                   -- which digest it appeared in, NULL if never shown
  feedback       INTEGER DEFAULT 0,      -- -1 / 0 / +1, set by you
  dupe_of        TEXT                    -- url_hash of the item this near-duplicates
);
CREATE INDEX idx_items_digest ON items(digest_date);
CREATE INDEX idx_items_seen   ON items(first_seen_at);
CREATE INDEX idx_items_dupe   ON items(dupe_of);

CREATE TABLE sources (
  name TEXT PRIMARY KEY, kind TEXT, url TEXT, etag TEXT, last_modified TEXT,
  last_success_at TEXT, consecutive_failures INTEGER DEFAULT 0
);
```

**Amended in phase 2, deliberately — do not "restore" these from an earlier draft:**

- `dupe_of` added. Near-duplicates are stored and flagged rather than dropped, so it stays
  possible to see what got collapsed. A column rather than a key inside `raw_json`, because
  `raw` is the source's payload verbatim and writing our own marker into it would break that.
- `sources.enabled` **removed**. `sources.yaml` is the single source of truth for whether a
  source runs — the same reason `SourceHealth` carries no `enabled` field. A column nothing
  reads is worse than an absent one: eventually something reads it and the two disagree. If
  auto-disable-after-N-failures ever ships it arrives as `auto_disabled_at`, a name that
  cannot be mistaken for config intent.
- `digest_date` is what defines "new": `unrendered_items()` selects `digest_date IS NULL`,
  never a comparison against today's date. `first_seen_at` is UTC while the digest day is
  America/Vancouver, so a 07:00 local run at 14:00 UTC would split one morning across two UTC
  days; and a missed run would lose those items permanently instead of catching up.
  *(Named `new_items()` here until 2026-09-15: "new" also named the count of rows written this
  run, and the two routinely disagree while both being correct. `insert_items` returns the
  other one.)*

**Amended 2026-09-15 — schema v2, and how the schema changes from here.**

The `sources` row gained `last_failure_at` and `total_failures`, because the counters above
destroy a feed's history the moment it recovers: a source that fails every other morning
reads as perfectly healthy every time you look at it. `total_failures` only counts from v2
onward, so a back-filled row can show a total *below* its consecutive count; the footer
prints `total=≥N` in that case rather than inventing a number, and the marker disappears on
its own once the true count overtakes.

Two rules that arrived with it:

- **`SCHEMA_VERSION` + `_MIGRATIONS` in `store.py`, keyed by the version migrated *from*,**
  applied in one explicit `BEGIN`/`COMMIT`. A database written by a newer schema is refused
  rather than opened, since this file is meant to be committed from a GitHub Action and a
  half-upgraded database is worse than a missing one. No Alembic — CLAUDE.md forbids it and a
  dict of DDL strings is the whole feature at this size.
- **`tests/fixtures/schema_v1.sql` is frozen and append-only.** It was extracted from git
  history, not regenerated, and it is the only proof the migration runs against a real v1
  database rather than against today's DDL with the new columns removed. Regenerating it from
  the current schema would make the migration test pass by construction while testing nothing.
  Add `schema_v2.sql` when v3 lands; never edit v1.

Keeping `raw_json` is the single most useful decision in this schema: when you change the ranker in
week 3 you can re-score three weeks of history offline instead of waiting three weeks to see if the
change helped.

---

## 5. Ranking design

Three escalating versions. Ship v1, use it, then decide if v2 earns its cost.

**v1 — rules (phase 3, zero cost).** Score = source weight + keyword hits − anti-keyword hits +
recency bonus + engagement bonus (HN points, GitHub stars-today, HF paper upvotes, normalised per
source). Assign `topic` by first matching keyword group. Then apply **per-topic quotas** — e.g. LLM 6,
agents 4, hardware 3, games 3, papers 3, releases "all of them, they're rare and always relevant".
Quotas are what stop the AI sections from eating the digest, and they're why a pure global ranking
would disappoint you.

**v2 — LLM ranking (phase 4).** Send *title + source + one-line context* for the ~60 survivors of v1
in a single batched call with your interest profile in the system prompt; ask for
`[{id, topic, score 0-10, one_clause_reason}]` as a structured output. Titles only — never full
article text — keeps this at a fraction of a cent. Keep the rule scores; use the LLM score as a
re-rank of the top 60, not a replacement. When they disagree strongly, that's your eval signal.

**v3 — embeddings (optional, after course week 5).** Embed the interest profile and each title;
cosine similarity as one more feature. Worth it mainly for catching relevant items whose titles share
no keywords with your profile. Do it as the course exercise, keep it only if the A/B is convincing.

### 5.1 Cross-source duplicates — the phase 3b task

Phase 3a confirmed on real data that `hf_papers` and `arxiv_cs_ai` carry the same paper on
the same morning, under different URLs:

```
[hf_papers]   https://huggingface.co/papers/2609.13141
[arxiv_cs_ai] https://arxiv.org/abs/2609.13141
```

**Prefer an extracted identifier over fuzzy matching wherever a source exposes one.** Those
two URLs both end in the arXiv ID. They are not similar documents, they are *the same
document*, and a shared extractable ID is the strongest duplicate signal available anywhere
in this system — deterministic, immune to a rewritten title, and with none of Dice's
false-positive surface. So layer it:

1. **ID identity pass, first.** Extract an arXiv ID from either URL shape; equal IDs set
   `dupe_of` deterministically.
2. **Dice + numeric guard, as fallback**, for pairs with no shared identifier — the same
   story on two news sites, which is the case it was designed for.

**Do not fold the ID into `canonical_url`**, tempting as it is. Collapsing at `url_hash` is a
silent drop through `INSERT OR IGNORE`, which is exactly the direction phase 2 refused. Keep
both rows, record the relationship, stay visible.

**Precedence must be explicit, and needs a load-bearing test.** Today the winner is whichever
source appears first in `sources.yaml`, which is incidental ordering in a file that looks like
formatting. Write a test that reorders `sources.yaml` and asserts the winner does not move —
even once the ID rule makes the question mostly moot, because "mostly" is where this class of
bug lives. On merit `hf_papers` should win: it carries `upvotes`, `submittedBy`, `numComments`
and `githubRepo`, i.e. it already survived a human filter, while arXiv cs.AI is ~240 unfiltered
entries a day. The exception is a zero-upvote HF entry, which carries no more signal than the
arXiv row — so make the tiebreak upvotes-aware rather than a flat source precedence.

**Feedback loop.** The `feedback` column plus a two-second way to set it (a `[+]`/`[−]` link per item
in the HTML that hits a tiny local endpoint, or just `digest feedback 41 -1` on the CLI). Thirty
labelled items is enough to tune keyword weights by hand; two hundred is enough to be worth fitting
something. Don't build the learning loop before you have the labels.

---

## 6. Summarisation design

Summarise **only the top N** (start N=8, tune to what you actually read). For each: what it is in one
sentence, why it matters *to you* in one clause, and the link. The "why it matters to you" is the whole
product — a generic summary is something you could already get from the headline.

Ask for structured output (`pydantic` model: `what`, `why_for_me`, `topic`, `confidence`) rather than
prose, so rendering stays a template concern. Use the cheapest capable model; this is a
titles-and-snippets task, not a reasoning task. Do **not** fetch and summarise full article bodies in
v1 — it multiplies cost and latency by ten for a marginal quality gain, and paywalls will fight you.

Cost sanity check: ~60 titles for ranking + 8 short summaries ≈ well under a cent per day on a small
model. If you're spending more than CAD $3/month, something is wrong with the design, not the pricing.

---

## 7. Phased build

Each phase is independently useful and independently abandonable. Target: phases 0–3 in the first
weekend, 4–5 over the following two weeks, 6+ only if you still want it.

### Phase 0 — Repo and skeleton (1 hour)
- `AI-news` repo: `uv init`, `src/digest/`, `pyproject.toml`, `.env.example`, `sources.yaml`,
  `interests.yaml`, `README.md`.
- CLI with subcommands from the start: `digest fetch`, `digest rank`, `digest render`, `digest run`.
  Separate commands are what let you iterate on ranking without re-fetching.
- **Done when:** `uv run digest --help` works and the repo has one commit.

### Phase 1 — One source, end to end (2 hours)
- Hacker News only. Fetch → `Item` → print titles. No database yet.
- **Done when:** `digest fetch --source hn` prints 30 titles with links.
- *Amended 2026-09-15: there is no `--source` flag and never was.* Which sources run is
  `enabled` in `sources.yaml` — one place that answers the question for the scheduled 07:00
  run and for you at a prompt, rather than two that can disagree. `digest fetch` did print 30
  titles with links, and the phase is done. A flag may still arrive for one-source debugging;
  if it does it must narrow a run, never define one.

### Phase 2 — Store and dedupe (2 hours)
- SQLite schema, canonical-URL hashing, `INSERT OR IGNORE`, `first_seen_at`.
- **Done when:** running it twice in a row inserts zero rows the second time, and
  `digest render` outputs only items first seen today.
- *Amended 2026-09-15: the second clause was replaced during the phase, not missed.* `render`
  outputs items whose `digest_date IS NULL` — never shown before — not items first seen today.
  Two reasons, both found by writing it: `first_seen_at` is UTC while the digest day is
  America/Vancouver, so a 07:00 local run splits one morning across two UTC days; and a
  missed run would silently lose a day's items instead of catching up the next morning. §4
  carries the same correction. The first clause holds and is tested.

### Phase 3 — Five sources, rule ranking, Markdown out (a weekend)
- Add GitHub Trending, HF daily papers, the AI-blogs bundle, arXiv cs.AI.
- Per-source adapters behind one interface; one failing source must not abort the run
  (wrap each in try/except, record the failure, continue — and put the source-health footer in).
- Rule-based scoring + per-topic quotas; render `digests/2026-09-14.md`.
- **Done when:** one command produces a dated Markdown file you'd be willing to read.
- **Then stop building and read it manually for four mornings.** This is the most important step in
  the plan and the easiest to skip. You are looking for: which sources you always skip, whether
  titles alone are enough (often they are), how many items feels right, and which of your four topics
  is actually starving.
- *Amended 2026-09-15: this phase split into 3a / 3b / 3c, and an interphase review ran
  between them.* **3a shipped** — five adapters, the resilient fetch loop, the health footer;
  425 rows over the recorded fixtures. **R0–R3 then ran** (§7.1) on the grounds that four more
  adapters had landed in a shape one adapter's design had chosen. **3b owes** rule ranking,
  per-topic quotas, cross-source dedupe by arXiv ID (§5.1) and the
  `REQUEST_WINDOW_HOURS >= max_age_hours` assertion (LC-6). **3c owes** the jinja2 Markdown
  render, `stamp_digest_date`, `render` tests (TS-2) and the orchestrator (LC-3). The
  "Done when" above belongs to 3c, and the four-morning read follows it — not 3a.

### Phase 4 — LLM ranking + summaries (matches LLM Eng week 1–2)
- Batched structured-output ranking call over the v1 survivors.
- Structured summaries for the top N.
- Cost logging per run (tokens in/out, model, cents) — a line in a `runs` table.
- **Done when:** cost per run is logged and under a cent, and the top three items are ones you'd have
  picked yourself on at least four days out of five.

### Phase 5 — Schedule and deliver (matches LLM Eng week 4)
- Windows Task Scheduler or WSL cron at 07:00 America/Vancouver; or a GitHub Action in the `AI-news`
  repo with the SQLite file committed back (works, is free, gives you history in git, and means the
  digest runs whether or not the laptop is on — my recommendation).
- Delivery: write `digests/YYYY-MM-DD.md` to the repo; then one push channel — Pushover (the course
  uses it in LLM Eng W8 D3 / Agents W1 D5, so it doubles as coursework) or plain SMTP email.
- Retry/backoff on fetch; never let one dead source fail the job.
- ⚠️ **If you commit `digest.db` back from a GitHub Action: checkpoint the WAL first.** The
  store opens SQLite in WAL mode, so a run's most recent transactions can still be sitting in
  the `digest.db-wal` sidecar when the commit step runs. Committing the `.db` alone then
  produces a database that is silently *behind* — it opens fine, looks fine, and is missing
  the last run. Either `PRAGMA wal_checkpoint(TRUNCATE)` before committing, or guarantee a
  clean `conn.close()` and commit the sidecars too. This is exactly the failure mode the
  source-health footer cannot catch, because the run itself succeeded.
- **Done when:** it has run unattended for seven consecutive days with no manual intervention.

### Phase 6 — Quality loop (ongoing, optional)
- Feedback capture and a weekly "what I marked down" review.
- Weekly roll-up digest (Sunday): the week's top ten across all topics.
- Embedding-based similarity as a ranking feature (course week 5 exercise).
- Near-duplicate clustering across outlets — **this is the shared ingestion layer with idea 04**; when
  you build it, build it in a way idea 04 can import rather than copy.

### 7.1 Interphase review R0–R3 (2026-09-14 → 2026-09-15)

Run between 3a and 3b, on the grounds that four adapters had landed in a shape one adapter's
design had chosen and nothing had yet asked whether that shape survived contact.

**Where it lives — `docs/reviews/`, and that is the canonical record, not this section.**

| File | What it is |
|---|---|
| Six dimension reports | Layering, duplication, silent failure, test-suite health, vocabulary, documentation drift. Written before any fix, each forbidden from editing code |
| `triage.md` | **The decision record.** All 54 findings, each fix-now / fix-later-with-a-trigger / won't-fix, grouped into seven themes. Read this before proposing a change to anything the review touched |
| `found-during-r3.md` | Things noticed while applying a theme, outside that theme's scope |
| `tests/snapshots/pipeline.txt` | R0's output: 425 rows, fetch → store over the recorded fixtures with the clock pinned. The contract every theme had to leave unchanged |

**What the seven themes changed**, one line each, for orientation only:

1. **Test-suite determinism** — four tests that would have gone red on a date in 2026-09-21 now pin their clock to `manifest.json`; the pinning test made load-bearing.
2. **`SourceHealth` split** — the per-run event (`SourceOutcome`) separated from the persisted state; schema v2 adds `total_failures` / `last_failure_at` so history survives recovery (§4).
3. **Unattended-run signals** — exit codes 0/2/3/4/70/130, the run log made visible without `-v`, `render` refusing to invent a database, and a footer whose five states are distinguishable.
4. **Exception taxonomy** — `digest/errors.py`: `DigestControlError` outside `Exception`, `AdapterError` as a classification (never a narrowed catch), and errors that name the datum that broke.
5. **Dispatch** — `kind` became the required dispatch key with one adapter instance per source; `known_kinds` injected into `load_config`.
6. **Shared helpers** — one datetime invariant, one title rule, `fetch_limit` enforced once in `fetch.py`, and the two senses of "new" given separate words (`insert_items` / `unrendered_items`).
7. **Documentation** — this section, the amendments above, README, and `adapters/base.py`.

**Why §7 above carries dated amendments instead of corrections, and the general rule.**

Rewriting a "Done when" line to match what shipped would erase the only evidence that the
belief and the outcome ever differed — and in a project whose recurring bug class is two
individually-correct decisions colliding, that gap is the most informative thing on the page.
So:

> **A record of intent gets an amendment. A catalogue of measurements gets a correction.**

§7 and §3's design intentions are records: append, never overwrite. §2's counts are
observations: correct them, and date both the old value and the new, because a number without
a date reads as a constant. §4 sits in between and already uses the amendment shape. Code
documentation — `base.py`, README — is neither: it describes what is true now, and stale
prose there is a defect rather than a historical record.

**Still deferred, with the trigger and where the trigger is planted.** `triage.md` holds the
reasoning; the point of the third column is that nothing here depends on anyone rereading it.

| Deferred | Trigger | Planted in |
|---|---|---|
| Bundle fan-out extraction (DUP-1, DUP-5) | The third RSS bundle adapter | `adapters/_mapping.py` docstring |
| Orchestrator module (LC-3) | 3c, composing the third stage | `cli.py::_run_fetch` docstring |
| Run provenance / `runs` table (SF-4, SF-5) | Phase 4 | `models.py::SourceOutcome.expected_failure` |
| Timeout-expiry test (TS-6) | Next change to timeout handling | `fetch.py::SOURCE_TIMEOUT_SECONDS` |
| `render` tests driving `main()` (TS-2) | 3c | `cli.py::_run_render` docstring |
| Result-type naming (VN-7) | A fourth result type | `models.py::SourceOutcome.expected_failure` |
| 429 backoff (DD-3) | First 429, or adding Reddit | CLAUDE.md, "Not yet implemented" |
| Conditional-GET validators (DD-7) | A host rate-limiting us | §3 above; `ai_blogs.conditional_headers`; `test_conditional_headers_are_empty_until_validators_are_persisted` |
| Ingest-window assertion (LC-6) | 3b (already owed) | `adapters/hn.py::REQUEST_WINDOW_HOURS` |
| Per-source fixture `captured_at` (R3-1) | Any edit to the recorder | `scripts/record_fixtures.py::write_manifest` |
| Skip-and-count unmappable entries (R3-3) | DUP-5, or the first real mapping failure | `errors.py::unmappable_entry` |

---

## 8. Testing and verification

Light, but not zero — this thing runs unattended, which is exactly where silent failure lives.

- **Adapter tests against recorded fixtures.** Save one real response per source into `tests/fixtures/`
  and assert the adapter produces the expected `Item` fields. When a source changes its shape, this is
  what tells you, and it's the only test that will ever pay for itself here.
- **A dedupe test**: same URL with different `utm_` params must collapse to one row.
- **A "no source can kill the run" test**: monkeypatch one adapter to raise; assert the digest still
  renders and the footer reports the failure.
- **Manual eval, weekly**: take one week of digests, mark each item keep/drop, compute what fraction of
  your "keeps" were in the top 8. That number is the only quality metric that matters; write it down
  each week so you can tell whether changes help.
- **Snapshot the renderer**: one golden Markdown file so template edits don't silently break layout.
- **The pipeline snapshot** (added by R0, 2026-09-14): `tests/snapshots/pipeline.txt` is fetch →
  store over every recorded fixture, clock pinned to `manifest.json`'s `captured_at`, dumped as
  425 sorted rows — url_hash, source, published_at, dupe_of, a hash of `raw`, title, url. It is
  a *characterisation* test: it asserts nothing about what is correct, only that composition has
  not moved, which is what makes it the contract a refactor is measured against. Every one of
  the seven R3 themes was required to leave it byte-identical.
  **Updating it is a two-step act, deliberately.** Regenerating with
  `uv run python -m scripts.snapshot_pipeline` (writing is the default; `--stdout` prints
  instead, which is the safe way to look first) is only legitimate *after* the behaviour
  change it reflects has been agreed, and the commit message must say what moved and why. A
  regenerated snapshot that nobody explained is a refactor with its evidence deleted. It is also
  reproducible only against the fixture set that produced it — re-record with `--source all`, or
  the pinned clock moves under the time-windowed adapters (see R3-1).

### 8.1 Review questions for any change

This project's bugs have not been logic errors. Every one so far came from an interaction
between two individually-correct decisions, and each surfaced from a check rather than a
claim. Ask these before declaring a change done:

1. **For every filter, what downstream measurement reads the filtered data, and does it need
   the unfiltered version?** A filter changes what every later measurement can see. Phase 3a:
   a 30-day ingest cutoff and a staleness warning were both correct, and composing them would
   have blinded the warning precisely on the feeds it existed to catch — a 112-day-stale feed
   reports "0 entries", which reads as a quiet week. Fixed by measuring `newest` over every
   entry before the cutoff.
   **This applies immediately in phase 3b**: `max_age_hours` is the next filter, and the
   UNCLASSIFIED count is the measurement behind it.
2. **Does any behaviour rest on an incidental property that looks like formatting?** Phase 3a:
   the direction of `dupe_of` between `hf_papers` and `arxiv_cs_ai` currently depends on the
   order of entries in `sources.yaml` — a file nobody thinks of as semantically load-bearing,
   which someone will alphabetise one day. See §5.1.
3. **Do two correct rules meet anywhere?** Phase 1: "no source may abort the run" had quietly
   generalised to "no signal may abort the run", so the test-suite network guard was caught
   and filed as a dead feed.
4. **Does the guard have a load-bearing test** — one that demonstrates the failure it
   prevents, so deleting it produces an explanation rather than silence?

---

## 9. Risks and how this plan handles them

- **Scope creep into a web app.** The plan's delivery is a Markdown file in a git repo. Resist the UI
  until phase 5 has run for a week. If you want it pretty, HTML email or a static page — not a service.
- **Source rot.** Handled by the health record and footer. Budget an hour a month for feed repair.
- **Over-engineering the ranker before you know what you like.** Phase 3's four-morning manual read is
  the deliberate guard. Rules first; the LLM ranker exists to be compared against something.
- **Scraping blocks.** GitHub Trending and Reddit both dislike default User-Agents. Set a real UA,
  cache aggressively, back off on 429, and prefer mirrors. You have the anti-bot lessons from JHA —
  reuse them, but don't import JHA's full stack for this.
- **Signal dilution as you add sources.** Every source addition should be paired with a quota and a
  two-week trial; if you don't read it, cut it. Ten good sources beat forty.
- **The digest becoming another feed you don't read.** The mitigation is the weekly keep/drop eval and
  being ruthless about the anti-topics list. If after a month you're not reading it, the honest move is
  to shrink it to five items and one topic rather than to add features.
- **Coursework collision.** Weeks 6–7 of LLM Engineering are heavy; this project should be finished
  (phase 5 done) by week 4 as the timing plan schedules it, precisely so it isn't competing then.

---

## 10. First working session — concrete checklist

1. `cd E:\workSpace\solo projects\AI-news` — `uv init`, commit the skeleton.
2. Copy `interests.proposed.md` → `interests.yaml`, edit until the topic list and anti-topics are
   genuinely yours. Fifteen minutes here beats two days of ranker tuning later.
3. `sources.yaml` with the five tier-1 sources, each with `name`, `kind`, `url`, `weight`, `quota`.
   *Amended 2026-09-15: the per-source cap is `fetch_limit`, not `quota`. `quota` was reserved
   for the per-topic cap in `interests.yaml`, because the two bound different things at
   different stages — ingest against selection — and one word for both is how you end up
   fetching narrowly when you meant to select narrowly.*
4. HEAD-check all five endpoints from your own machine before writing an adapter against any of them.
5. Build the HN adapter only. Print titles. Commit.
6. Add SQLite + dedupe. Run twice, confirm zero new rows. Commit.
7. Add the remaining four adapters one at a time, committing each.
8. Rule ranker + quotas + Markdown render.
9. **Read the output every morning for four days before writing any LLM code.**

---

## 11. Open decisions for you

- **Where do you want to read it?** Markdown file in the repo, email, Pushover push, or a static
  page. The plan assumes file-first; the delivery channel changes phase 5 and nothing else.
- **Laptop cron or GitHub Action?** Action is more reliable and free; laptop keeps everything local
  and needs no secrets in a repo. I lean Action.
- **English only, or Chinese sources too?** Adding 机器之心 / 量子位 / Chinese hardware media roughly
  doubles the source count and would make the digest bilingual. Worth deciding now — it affects the
  summariser prompt and whether the interest profile needs bilingual keywords.
- **Does this stay solo, or is it the ingestion layer for idea 04?** If the latter, the store goes
  Postgres at phase 6 rather than SQLite. Either answer is fine; drifting between them is not.

---

## Sources consulted

- Hugging Face daily-papers API — <https://huggingface.co/api/daily_papers> (verified)
- Hugging Face models API — <https://huggingface.co/api/models> (verified)
- HN Algolia API — <https://hn.algolia.com/api/v1/search> (verified)
- arXiv RSS — <https://rss.arxiv.org/rss/cs.AI> (verified, same-day)
- Olshansk/rss-feeds (hourly AI-blog RSS mirrors) — <https://github.com/Olshansk/rss-feeds> (verified)
- taobojlen/anthropic-rss-feed — <https://github.com/taobojlen/anthropic-rss-feed>
- mshibanami/GitHubTrendingRSS — <https://github.com/mshibanami/GitHubTrendingRSS>
- vitalets/github-trending-repos — <https://github.com/vitalets/github-trending-repos>
- zernel/huggingface-trending-feed — <https://github.com/zernel/huggingface-trending-feed>
- RSSHub — <https://github.com/DIYgod/RSSHub>
- Steamworks ISteamNews docs — <https://partner.steamgames.com/doc/webapi/ISteamNews>
