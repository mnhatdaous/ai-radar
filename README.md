# ai-radar

A daily crawler for everything happening in AI. Each morning it reads about 80 sources — lab blogs, newsletters, news outlets, research papers, tools, models, repos, releases and community threads — throws away what is old, off-topic or already seen, merges duplicates, and writes:

```
digests/2026-11-10.md        the day's digest, ranked and grouped (digests/latest.md is always the newest)
data/items/2026-11-10.jsonl  every new item as JSON Lines: the durable, greppable archive
feeds/ai-radar.atom          the best items of the last 7 days, for any RSS reader
https://<you>.github.io/<repo>/   the same digests as a website (GitHub Pages, rebuilt after each crawl)
```

Plain files in git. No server, no accounts, no API keys. A run takes about a minute.

```markdown
# AI Radar · 2026-11-10

> **557 new items** grouped into **512 stories** · paper 168 · news 116 · blog 90 · repo 49 · … · 80 of 80 sources ok

## Top stories
1. **[Title of the story](https://…)** — Source · 9h ago · `llm` `agents`
   - A one-line summary taken from the source.
   - HN 118 pts · 144 comments · discussion: [HN](https://news.ycombinator.com/item?id=…) · also: [r/LocalLLaMA](https://…)
   - covered by [The Verge](https://… "Another outlet's headline"), [TechCrunch](https://…) ×2, [r/ClaudeAI](https://…) ×3
## News (86)  ·  Labs, companies and newsletters (80)  ·  Research papers (168)
## Tools and launches  ·  Open-source repos  ·  Models and datasets  ·  Releases  ·  Discussions
### Source health        <- only when something failed
```

A real one is in [`digests/`](digests/).

## Quick start

You need [uv](https://docs.astral.sh/uv/) (`brew install uv`); it installs Python for you.

```bash
uv sync                    # install
uv run ai-radar daily      # crawl everything and write today's digest
open digests/latest.md
```

The first run treats the last 3 days as new. To start with more history: `uv run ai-radar daily --max-age-days 14`.

## Run it every day

### Option A: GitHub Actions (set and forget)

1. Push this repo to GitHub.
2. That is all: [`.github/workflows/daily.yml`](.github/workflows/daily.yml) runs every day at **00:23 UTC** (07:23 in Vietnam), then commits the new `digests/`, `data/items/` and `feeds/` files back to the repo. Try it right away from the **Actions → daily → Run workflow** button.

Things to know:

- Change the time by editing the `cron` line.
- If the push step fails with a 403, enable **Settings → Actions → General → Workflow permissions → Read and write**.
- Reddit and three Substack newsletters (`import-ai`, `zvi`, `gary-marcus`: `*.substack.com` answers 403) block GitHub's servers, so the workflow runs with a `--skip` for each. They work from a home connection, so run `scripts/daily.sh` on your Mac if you want them; the archive merges both. Remove a flag to try that source from GitHub anyway.
- The first run from GitHub's servers is the real test: some sites treat datacenter IPs differently from your home connection. Anything that fails shows up under "Source health"; for a persistent offender use `--skip ID` in the workflow or `enabled: false` in the config.
- The job fails (and GitHub emails you) only when *every* source failed. One broken source never stops a run; it shows up under "Source health" in that day's digest.
- GitHub pauses scheduled workflows on public repos after 60 days without repo activity. The daily commits normally keep it alive; if runs ever stop, look at the Actions tab.

### The report as a website (GitHub Pages)

After each crawl the workflow also builds a small static site from the digests and publishes it to the `gh-pages` branch with `peaceiris/actions-gh-pages`. You get one page per day, an archive, the Atom feed, and a filter box on every page.

- `https://<you>.github.io/<repo>/` is the newest digest, `…/archive.html` lists every day and `…/ai-radar.atom` is the feed.
- One-time setup: after the first workflow run has created the `gh-pages` branch, open **Settings → Pages**, choose **Deploy from a branch**, then `gh-pages` and `/ (root)`.
- GitHub Pages needs a **public** repo on a free account.
- To see it locally: `uv run ai-radar site && open site/index.html`.
- The site shows headlines, short excerpts and links, the same material the sources put in their own feeds, and every item links back to its source.

### Option B: on your Mac (launchd)

```bash
sed "s#__REPO__#$PWD#g" deploy/com.ai-radar.daily.plist > ~/Library/LaunchAgents/com.ai-radar.daily.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ai-radar.daily.plist
launchctl kickstart gui/$(id -u)/com.ai-radar.daily     # run once now; output goes to data/daily.log
```

It runs at 07:30 local time (edit the plist to change it) and, if the Mac was asleep, as soon as it wakes. Remove it with `launchctl bootout gui/$(id -u)/com.ai-radar.daily`.
[`scripts/daily.sh`](scripts/daily.sh) is the entry point; it also works from cron (`30 7 * * * /path/to/ai-radar/scripts/daily.sh`). Put `AI_RADAR_COMMIT=1` in a git-ignored `.env` file to have it commit and push the results after each run.

Reddit works best here, from a home connection.

### Both at once, or missed days

Running locally *and* in Actions is fine: the JSONL archive is the shared record, and each machine rebuilds its local database from it (`git pull` before a local run). A run looks back 3 days by default (some slow blogs look further), so a short outage heals itself. After a longer gap, run `uv run ai-radar daily --max-age-days 14`.

## What it crawls

80 sources are enabled; `uv run ai-radar sources` lists them all.

| Group | Examples | How |
|---|---|---|
| AI labs and companies | OpenAI, Anthropic, Google DeepMind, Google AI and Research, Meta AI, Mistral, xAI, DeepSeek, Cohere, Hugging Face, Ai2, Apple ML, Microsoft Research, NVIDIA, AWS, Cloudflare, GitHub | RSS; for labs without a feed (Anthropic, xAI, Cohere, DeepSeek, Meta AI): their sitemap or blog index, then each new page's metadata |
| Newsletters and expert blogs | Import AI, Latent Space, Interconnects, Simon Willison, Ahead of AI, Lilian Weng, Karpathy, Zvi, SemiAnalysis, Epoch AI, TLDR AI | RSS |
| News outlets | TechCrunch, The Verge, Ars Technica, WIRED, MIT Technology Review, The Guardian, The Register, IEEE Spectrum, The Decoder; BBC, NYT and CNBC tech feeds, filtered down to AI | RSS |
| Research | arXiv (cs.AI, cs.CL, cs.LG, cs.CV, cs.MA), Hugging Face Daily Papers, Nature | API, RSS |
| Tools and launches | Product Hunt (AI), Show HN, trending Hugging Face Spaces | RSS, API |
| Models | New models on OpenRouter, trending Hugging Face models and datasets | API |
| Repos | New GitHub repos by topic (llm, ai-agents, mcp, rag…), GitHub Trending | API, HTML |
| Releases | 32 projects, e.g. Ollama, vLLM, transformers, claude-code, codex, LangGraph | Atom |
| Community | Hacker News (19 AI queries), Lobsters, Reddit (5 subreddits), DEV | API, RSS |

Off by default, with a comment in the config saying why: **GDELT** (broad news coverage, but rate-limited and slow) and two **YouTube** channels (YouTube's feeds answer 404 for hours at a time). Turn any of them on with `enabled: true`.

## How it works

```
sources (all at once)  →  normalize            →  merge duplicates   →  store            →  outputs
rss · sitemap · html      clean the URL           one link seen by      SQLite cache         digest (Markdown)
hackernews · arxiv        drop old items          several sources       + JSONL archive      archive (JSONL)
huggingface · github      drop non-AI items       = one item with                            feed (Atom)
openrouter · gdelt        tag topics              "mentions"

When a digest or the feed is built, items about the same event are then folded into one *story*.
```

- **New means new forever.** An item is identified by its canonical URL: tracking parameters, `www.`, trailing slashes and `http` vs `https` are ignored; an arXiv PDF, its abstract page and its Hugging Face paper page are one item; GitHub owner and repo names are case-insensitive. A URL is "new" only the first time it is ever seen.
- **One item, many sources.** If the same link arrives from OpenAI's blog, Hacker News and Reddit, the most trusted source (highest `weight`) owns the item and the others are recorded as *mentions*, with their points, ranks and thread links.
- **AI filter.** General sources (Hacker News, BBC, NYT…) set `ai_filter: loose`, which keeps an item when its title has one strong AI term (`AI`, `LLM`, `OpenAI`, `Claude Code`…) or two weak ones (`inference`, `agents`, `GPU`…). A passing mention in the summary does not count. The rules are plain regexes in [`config/topics.yaml`](config/topics.yaml), together with the topic tags (`llm`, `agents`, `coding`, `safety`, `policy`, `business`, `infra`…).
- **Ranking** is computed when a digest is rendered: `4 × source weight` + up to 3 points of recency (fading over ~2 days) + `1.2 × log10(1 + popularity)` + 0.8 per extra source that mentioned the link (max 4). Popularity is normalised across sources (HN points, GitHub stars gained today, Hugging Face upvotes, Reddit rank).
- **One event, many articles.** Outlets cover the same news under different URLs, so the digest groups items whose headlines share enough rare words (names, products, version numbers: `Fable`, `Mythos`, `5.2`) into one *story*. The most trusted article heads it (a Reddit or HN thread can join a story but never lead it), the other outlets are listed under it (`covered by The Verge, TechCrunch ×5 …`, each link's tooltip is the headline), and every extra outlet adds to the story's score, so widely covered events rise to the top. Only news, blogs, threads and videos are grouped; a paper, repo or model is never folded into anything. Grouping is a heuristic on headline words, which cannot tell an event ("Fable 5.2") from a compound noun ("data center"), so it is deliberately conservative and a few false merges remain; folded items are never hidden, only tucked under their story. Switch it off or tune it with `group_stories` and `story_overlap` in `settings.digest`.
- **Front page mix.** "Top stories" reserves slots (`top_mix`) for editorial items, community threads and builder items (papers, repos, models…), and no source may head more than two of the stories, so trending repos cannot crowd out the news.
- **State.** `data/items/*.jsonl` is the durable record and is committed. `data/radar.db` (SQLite, git-ignored) is a cache that is rebuilt from the archive whenever it is missing, so a fresh clone or CI runner loses nothing.
- **Failures stay local.** Each source runs in isolation with its own timeout. A broken source is reported, the rest carry on, and the exit code is non-zero only if everything failed (or with `--strict`).

### Good manners

The crawler identifies itself (`ai-radar/0.1`), reads `robots.txt` before fetching any HTML page, spaces out requests per host (and follows `Crawl-delay`), backs off on 429/5xx, and never logs in or tries to get around a paywall or a bot block: a site that answers 403 or 429 is reported and skipped. It stores titles, short excerpts (400 characters at most) and links, never whole articles.

## Commands

```bash
uv run ai-radar daily                       # crawl, then write digest + archive + feed  (what you schedule)
uv run ai-radar crawl --dry-run --show 3    # try every source, print 3 titles each, store nothing
uv run ai-radar crawl --dry-run --source arxiv --show 10
uv run ai-radar digest --date 2026-11-10    # rebuild a day's files from what is stored
uv run ai-radar search "context window" --kind paper --days 30
uv run ai-radar site                        # build the website into ./site (GitHub Pages serves it)
uv run ai-radar sources                     # list sources, weights and filters
uv run ai-radar stats                       # what is in the store
```

Flags worth knowing: `--source ID` (only this source, even a disabled one), `--skip ID` (leave one out), `--max-age-days N` (override the age window; feeds and sitemaps honour it, while Hacker News and GitHub search use their own windows), `--strict` (exit 1 if any source failed), `--root DIR` or `$AI_RADAR_ROOT` (where `config/` and `data/` live), `-v` (debug logging).

Set `GITHUB_TOKEN` to raise the GitHub search limit from 10 to 30 requests per minute: export it in your shell, or for scheduled local runs put `GITHUB_TOKEN=...` in a git-ignored `.env` file that `scripts/daily.sh` reads (Actions provides one automatically). Without it, running twice within a minute can hit the limit; that source then reports a partial failure and the rest of the run is unaffected.

## Configuration

Everything lives in [`config/sources.yaml`](config/sources.yaml) and [`config/topics.yaml`](config/topics.yaml). No code changes are needed to add a source.

```yaml
# a feed
- { id: my-blog, name: My Blog, type: rss, url: "https://example.com/feed.xml", kind: blog, weight: 0.6, ai_filter: loose }

# a lab without a feed: read its sitemap, then each new page
- id: some-lab
  name: Some Lab
  type: sitemap
  url: "https://some-lab.example/sitemap.xml"
  include: '^https://some-lab\.example/blog/[^/]+$'
  require_date: true     # skip hubs and landing pages: they declare no publish date
  kind: blog
  weight: 0.9

# release feeds of the projects you follow
- { id: my-releases, type: github_releases, kind: release, repos: [ollama/ollama, vllm-project/vllm] }
```

Check a new source before relying on it: `uv run ai-radar crawl --dry-run --source my-blog --show 5`.

**Fields every source has**

| Field | Meaning |
|---|---|
| `id`, `name`, `type` | identifier, display name, and one of the types below |
| `kind` | `news` `blog` `paper` `repo` `tool` `model` `dataset` `release` `discussion` `video`; decides the digest section |
| `weight` | 0–1 trust. Sets the base score, and the most trusted source owns a link several sources reported |
| `ai_filter` | `off` (default), `loose` (title + summary) or `strict` (title only) |
| `max_items` | most items kept per run (default 25) |
| `max_age_days` | ignore older items (default 3, from `settings.max_age_days`) |
| `enabled` | `false` switches a source off |

**Source types**

| `type` | Reads | Options |
|---|---|---|
| `rss` | RSS and Atom feeds | `url`; `flavor`: `plain`, `lobsters`, `reddit`, `producthunt` |
| `sitemap` | a sitemap (or index), then each new page's metadata | `url`, `include`; `exclude`, `require_date`, `max_pages` (12), `date_regex` + `date_format` (dates in URLs), `child_include`, `max_children`, `brand` |
| `html_list` | a listing page's links, then each new page's metadata | `url`, `link_regex`; `link_selector`, `max_pages`, `require_date`, `brand` |
| `hackernews` | Hacker News via Algolia | `queries`; `min_points` (20), `window_hours` (30), `hn_tag` (`story`, `show_hn`), `hits_per_page` |
| `arxiv` | newest submissions per category | `categories`; `max_results` (40 each) |
| `huggingface` | `daily_papers`, or trending `models`, `datasets`, `spaces` | `mode`; `limit`, `days` (daily papers) |
| `github_search` | repos created recently under topics | `topics` or `queries`; `created_within_days` (7), `min_stars` (25), `per_page` |
| `github_trending` | github.com/trending | `languages`, `since` |
| `github_releases` | release feeds of repos | `repos`; `max_per_repo` (2), `skip_prereleases` |
| `openrouter` | models newly listed on OpenRouter | none |
| `gdelt` | GDELT news search | `queries`; `timespan`, `max_records`, `exclude_domains` |

Page-based sources (`sitemap`, `html_list`, `github_trending`) respect `robots.txt` (switch that off with `settings.respect_robots: false` only for your own sites). `sitemap` and `html_list` never request a page that is already in the store.

**Settings.** `settings.hosts` sets the minimum seconds between requests and the maximum parallel requests per host; `settings.digest` controls the digest (`top_n`, `top_mix`, `max_per_section`, `summary_chars`, `group_stories`, and `story_overlap`: how alike two headlines must be to count as one story, 0.5 by default; lower groups more, higher groups less).

## Output formats

**Archive** (`data/items/YYYY-MM-DD.jsonl`, one JSON object per line, one file per UTC day, holding the items first seen that day). This is an illustrative record:

```json
{"id":"a4e01cee6aaca4a9","url":"https://example.com/post","canonical_url":"https://example.com/post",
 "title":"A new open model","summary":"Short excerpt…","author":null,
 "published_at":"2026-11-09T17:18:59Z","first_seen_at":"2026-11-10T00:23:41Z","last_seen_at":"2026-11-10T00:23:41Z",
 "source_id":"openai-news","source_name":"OpenAI","kind":"blog","weight":1.0,
 "topics":["llm"],"tags":[],"metrics":{"hn_points":70.0,"hn_comments":22.0},"engagement":81.0,
 "discussion_url":"https://news.ycombinator.com/item?id=1","domain":"example.com",
 "mentions":[{"source_id":"hn-ai","source_name":"Hacker News","url":"https://news.ycombinator.com/item?id=1","title":"…","metrics":{},"seen_at":"2026-11-10T00:23:41Z"}]}
```

```bash
jq -r 'select(.kind=="paper") | .title' data/items/2026-11-*.jsonl     # all November papers
sqlite3 data/radar.db "select source_name, count(*) from items group by 1 order by 2 desc limit 10"
```

**Atom feed** (`feeds/ai-radar.atom`): the 100 best stories of the last 7 days, one entry per story with the other coverage attached as `related` links. Subscribe to it in any RSS reader, or publish it with GitHub Pages.

**Size.** The archive grows by roughly 0.5 MB a day, about 135 KB once git compresses it (~50 MB a year). arXiv is a fifth of the volume: lower `max_results` or `max_items` on the `arxiv` source to shrink it. Gzipped months are still read, so `gzip data/items/2026-01-*.jsonl` slims your working copy (git history already stores files compressed).

## Extending

A new kind of source is one small class. Here is a complete one:

```python
# src/ai_radar/sources/my_source.py
from ai_radar.models import RawItem
from ai_radar.sources.base import Source, register
from ai_radar.text import parse_datetime


@register
class MySource(Source):
    type = "my_source"
    required = ("url",)  # options that must be present in the YAML

    async def fetch(self) -> list[RawItem]:
        data = await self.http.get_json(self.opt("url"))  # polite, retrying, per-host paced
        return [
            RawItem(
                url=row["link"],
                title=row["title"],
                summary=row.get("blurb", ""),
                published_at=parse_datetime(row.get("date")),
                engagement=float(row.get("votes", 0)),  # popularity, roughly "HN points"
            )
            for row in data["items"]
        ]
```

Add `my_source` to the import list in [`sources/__init__.py`](src/ai_radar/sources/__init__.py), then use `type: my_source` in the YAML. Age filtering, the AI filter, dedupe, topics, storage and the digest are applied for you. The tests show how to exercise a source against a fake internet (`tests/helpers.py`).

## Limits worth knowing

- **Not covered:** X/Twitter, LinkedIn, Discord and Telegram (no reliable public access), paywalled sites, and sites that block bots. VentureBeat and KDnuggets answered 403/429 to the crawler, so they are left out rather than worked around.
- **Story grouping is a heuristic.** It works on headline words, so it can miss that two headlines are the same event (different wording) or fold two related-but-different articles together (e.g. two pieces that both happen to be about data centers). A developing story can also split into a few groups. Everything folded stays one click away.
- **Some items carry no date.** Pages without publish metadata (parts of Meta AI's blog, GitHub Trending) show as "new" instead of "9h ago".
- **Reddit, GDELT and YouTube are the flaky ones.** Reddit answers 429 to a second request within a minute and often blocks cloud IPs; the config fetches all subreddits in one request to stay under that.
- **Days are UTC.** A "day" is a UTC day; the default schedule runs just after midnight UTC.

## Development

```bash
uv run pytest                                   # 247 offline tests, about three seconds
uv run ruff check . && uv run ruff format --check .
```

The tests never touch the network: `tests/helpers.py` provides a fake internet (`httpx.MockTransport`) that the HTTP client, every source, the pipeline and the CLI run against.

```
config/                  sources.yaml, topics.yaml
src/ai_radar/
  cli.py                 commands
  pipeline.py            one run: fetch → normalize → merge → store
  sources/               one module per source type (+ _page.py: page metadata)
  http.py                polite async HTTP: pacing, retries, robots.txt
  store.py               SQLite cache, archive sync, search
  stories.py             groups coverage of one event into a story
  site.py                the static website built from the digests (GitHub Pages)
  digest.py  export.py   Markdown digest · JSONL archive and Atom feed
  relevance.py  scoring.py  urls.py  text.py  config.py  models.py
tests/                   offline test suite
.github/workflows/       daily.yml (the schedule) · ci.yml (tests on push)
deploy/  scripts/        macOS launchd job · local runner
```

## Ideas for next

- Use the days of history already in the archive to tell event words from everyday words more precisely when grouping stories.
- A short LLM-written summary at the top of each digest.
- Delivery: post the top stories to Slack or email them.
- Full-text extraction for search over article bodies (store only what you may keep).
- Run the crawl as a scheduled Cloudflare Worker, with D1 or R2 instead of git for storage.
