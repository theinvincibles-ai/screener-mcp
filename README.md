# screener-mcp

<!-- mcp-name: io.github.LogeshR15/screener-mcp -->

An MCP (Model Context Protocol) server that gives Claude live access to [Screener.in](https://www.screener.in), NSE, and MCX data — turning Claude into a research assistant for Indian stocks.

[![PyPI](https://img.shields.io/pypi/v/screener-mcp)](https://pypi.org/project/screener-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/screener-mcp)](https://pypi.org/project/screener-mcp/)
[![CI](https://github.com/LogeshR15/screener-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/LogeshR15/screener-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[Report an issue](https://github.com/LogeshR15/screener-mcp/issues) · [LinkedIn](https://linkedin.com/in/logesh-ramasamy/) · logeshl2003@gmail.com

`screener-mcp` connects Claude (Claude Code, Claude Desktop, or any MCP client) to Indian equity data: company financials, stock screening, annual reports, earnings calls, corporate announcements, and a local portfolio tracker. Point Claude at a company or a screen, and it does the research using real, current data instead of its training-data knowledge of the stock.

---

## What you can do

```
"Compare ITC and HINDUNILVR on all key ratios"
"Find low-debt, high-ROCE chemical stocks"
"Quality stocks within 10% of their 52-week low"
"Has MSUMI fallen more than the auto sector over the last 60 days?"
"What are analysts' target prices for TMPV, and what's the latest news?"
"Summarize the key risks from Reliance's 2024 annual report"
"What did TCS management say about margins in Q3FY25?"
"What are the red flags in Asian Paints?"
"Show me recent NSE announcements for HDFCBANK"
"Find recent bulk deals in a stock"
"Track my portfolio and show live P&L"
"Save a research note on TITAN — strong Q3, watch margins"
```

---

## Quick start

Requires [uv](https://github.com/astral-sh/uv) — `pip install uv` or `brew install uv`.

```bash
claude mcp add screener -s user -- uvx --from 'screener-mcp[ai]' screener-mcp
```

This installs the full server, including document analysis (annual reports, earnings calls). If you only need company research and stock screening, drop the extra for a much lighter install:

```bash
claude mcp add screener -s user -- uvx screener-mcp
```

> **Core vs. `[ai]`:** the base package covers company data, screening, NSE announcements, and portfolio tracking. The `[ai]` extra adds `pdfplumber`, `chromadb`, and `sentence-transformers` (~1–2GB, via torch) to power `ask_company_research` and `search_market_commentary` (and the management-guidance part of `get_forward_outlook`). Without it, those tools return a "not installed" error — everything else works normally.

Using Claude Desktop instead of Claude Code? See [Claude Desktop setup](#claude-desktop).

---

## Verify it works

```bash
claude mcp list
# screener  stdio  Connected
```

Then try a few prompts in Claude:

```
"Search for Asian Paints"
"Give me the company overview for TCS"
"Run the defense theme screen"
```

If these return real data, the server is working end to end.

---

## What it provides

- **Company research** — financials (incl. year-end P/E, P/B, ROE and debt-to-equity history), shareholding trends with promoter/pledge assessment, peer comparison, rule-based red-flag checks (no login required)
- **Stock screening** — custom Screener.in-style queries with `AND` / `OR` / parentheses, pre-built thematic screens (sector themes start from the companies actually in the sector), and technical clauses (52-week distance, RSI, DMA, volume spikes). Junk rows are filtered out by default. Fundamental screens need a free Screener.in login; technical-only screens don't
- **Price-action context** — quality stocks near 52-week lows in one call, and a stock's move vs its sector index and the Nifty 50
- **Valuation & quality context** — P/E and ROCE vs the industry median for up to 20 stocks at once, and moat proxies: revenue share, rank, industry concentration, and how durable returns and margins have been
- **Forward-looking & Street view** — analyst EPS/revenue estimates and target prices, order wins and capex filings, management guidance from earnings calls, and recent news
- **Document analysis** — ask questions over annual reports and earnings call transcripts using a local RAG pipeline
- **Corporate events** — NSE announcements (incl. credit and ESG rating actions), bulk deals by company or investor, insider trading disclosures
- **Market & research** — commodity price context, local research notes
- **Portfolio** — a private, local holdings tracker with live P&L

34 tools in total — full reference [below](#tools--34-total). Every tool returns the same [response envelope](#response-envelope), so partial or degraded data is always explicit.

Two prompts — `deep_dive(symbol)` and `value_screen(min_roce, max_pe)` — package common research workflows; clients that support MCP prompts show them as slash commands.

Speaks MCP protocol **2026-07-28** (built on the v2 Python SDK) and still serves clients on 2025-11-25 and earlier.

---

## Example workflows

- **Compare companies** — `"Compare ITC and HINDUNILVR on all key ratios"` → `compare_companies`
- **Screen for opportunities** — `"Find low-debt, high-ROCE small caps"` → `screen_by_theme` or `screen_stocks`
- **Find pullbacks** — `"High-ROCE, low-debt stocks near their 52-week low"` → `get_52_week_low_candidates`, or `screen_stocks("Return on capital employed > 15 AND 52 week low distance < 10")`
- **Market vs company-specific** — `"Is this fall sector-wide or just this stock?"` → `compare_to_sector`
- **What the Street thinks** — `"Analyst targets and recent news for TMPV"` → `get_analyst_targets`, `get_recent_news`
- **Is it cheap for its sector?** — `"Which of these 15 stocks trade below their industry P/E?"` → `get_relative_valuation`, or `screen_stocks(..., peer_relative=True)`
- **Moat check** — `"Is MSUMI a market leader with durable returns?"` → `get_moat_signals`
- **Future growth** — `"What's the order pipeline and guidance for BEL?"` → `get_forward_outlook`
- **Read an annual report** — `"What are the key risks in Reliance's 2024 annual report?"` → `ask_company_research(..., year=2024)`
- **Read an earnings call** — `"What did TCS say about margins in Q3FY25?"` → `ask_company_research(..., quarter="Q3FY25")`
- **Across years** — `"How has ITC described cigarette taxation over the years?"` → `ask_company_research(..., doc_type="annual_report")`
- **Spot red flags** — `"What are the red flags in Asian Paints?"` → `analyze_red_flags`
- **Track NSE activity** — `"Show recent announcements for HDFCBANK"` → `get_company_announcements`
- **Research bulk deals** — `"Any recent bulk deals in TITAN?"` → `get_bulk_deals("TITAN")`; `"What has SBI Mutual Fund bought in bulk?"` → `get_bulk_deals(name="SBI Mutual Fund")`
- **Track a portfolio** — `"Add 10 shares of INFY at ₹1500 to my portfolio"` → `add_portfolio_stock`
- **Save research** — `"Save a note on TITAN — strong Q3, watch margins"` → `notebook_ai`

---

## Architecture

```
Claude (Code / Desktop)
        │  MCP
        ▼
  screener-mcp
        │
        ├──► Screener.in   (financials, ratios, screening, industry pages, price history)
        ├──► NSE India     (announcements, order wins, bulk deals, insider trades)
        ├──► Yahoo Finance (analyst consensus & estimates, commodity benchmarks)
        └──► Google News   (recent headlines, broker target mentions)
        │
        ▼
  Research data (parsed, cached, indexed)
        │
        ▼
  Claude reasons over the data and answers
```

`screener-mcp` fetches and normalizes the data; Claude does the analysis and explains it in plain language.

---

## Installation options

### Recommended

```bash
# Full install (company research, screening, documents, everything)
claude mcp add screener -s user -- uvx --from 'screener-mcp[ai]' screener-mcp

# Lightweight install (skip document analysis)
claude mcp add screener -s user -- uvx screener-mcp
```

### Claude Code

Use the `claude mcp add` commands above. Confirm with `claude mcp list`.

### Claude Desktop

Claude Desktop doesn't read `claude mcp add` — edit its config file directly.

**1.** Install [uv](https://github.com/astral-sh/uv) if needed: `brew install uv` (or `pip install uv`)

**2.** Open the config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

(In the app: **Settings → Developer → Edit Config**.)

**3.** Add the `screener` server:

```json
{
  "mcpServers": {
    "screener": {
      "command": "uvx",
      "args": ["--from", "screener-mcp[ai]", "screener-mcp"],
      "env": {
        "SCREENER_USERNAME": "your@email.com",
        "SCREENER_PASSWORD": "yourpassword"
      }
    }
  }
}
```

Claude Desktop does **not** inherit your shell environment, so credentials must go in the `"env"` block here — see [Credentials](#credentials). Leave `"env"` out entirely if you only want the no-login company-research tools.

> `spawn uvx ENOENT` on launch? Claude Desktop starts with a minimal `PATH`. Run `which uvx` and use the absolute path (e.g. `/opt/homebrew/bin/uvx`) as `"command"`.

**4.** Quit Claude Desktop completely (**Cmd+Q** on macOS) and reopen it.

**5.** Check the tools icon in the message composer — `screener` should list its 34 tools. Then ask: `"Search for Asian Paints"`.

> Server not showing up? Check **Settings → Developer** for its status, and the logs at `~/Library/Application Support/Claude/logs/mcp-server-screener.log` (macOS) or `%APPDATA%\Claude\logs\` (Windows). Invalid JSON — often a stray trailing comma — makes Claude Desktop skip every server silently.

### pip / PyPI

The package is published on [PyPI](https://pypi.org/project/screener-mcp/) as `screener-mcp`. `uvx` (above) runs it without a persistent install; to install it into an environment instead:

```bash
pip install screener-mcp          # core
pip install "screener-mcp[ai]"    # with document analysis
```

Then run it directly, or point `claude mcp add` at the `screener-mcp` entry point it installs.

### Developer (local clone)

```bash
git clone https://github.com/LogeshR15/screener-mcp
cd screener-mcp
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

claude mcp add screener -s user -- \
  $(pwd)/.venv/bin/python3.11 \
  $(pwd)/run_server.py
```

> Any Python **3.11+** works — e.g. `python3.12 -m venv .venv` — just point `claude mcp add` at that interpreter.
> `pip install -e .` failing with "editable mode currently requires a setuptools-based build"? Upgrade `pip` in the venv first (as above), then retry.

**Already cloned it and using Claude Desktop?** Point the config at your venv interpreter instead of `uvx`:

```json
{
  "mcpServers": {
    "screener": {
      "command": "/absolute/path/to/screener-mcp/.venv/bin/python3.11",
      "args": ["/absolute/path/to/screener-mcp/run_server.py"]
    }
  }
}
```

### Advanced: HTTP and Docker

For remote/network deployment rather than a local stdio process, see [Remote HTTP server](#remote-http-server) and [Docker](#docker) below.

---

## Credentials

| Capability | Needs Screener.in login? |
|---|:---:|
| Company research (financials, ratios, shareholding, peers, red flags) | No |
| Stock screening with fundamental clauses (`screen_stocks`, `screen_by_theme`) | Yes |
| Technical-only screens, `get_52_week_low_candidates`, `compare_to_sector` | No (a login lets `get_52_week_low_candidates` scan the whole market instead of an index) |
| `get_relative_valuation`, `get_moat_signals`, `get_forward_outlook`, `get_analyst_targets`, `get_recent_news` | No (`get_forward_outlook`'s earnings-call guidance needs the `[ai]` extra) |
| NSE announcements, bulk deals, credit ratings, commodities | No |
| Document analysis, notebook, portfolio | No |

**To enable screening:**

**1.** Register free at [screener.in/register](https://www.screener.in/register/)

**2. Claude Code / manual runs** — add to `~/.zshrc` or `~/.bashrc`, then reload (`source ~/.zshrc`) and restart Claude Code:

```bash
export SCREENER_USERNAME="your@email.com"
export SCREENER_PASSWORD="yourpassword"
```

**3. Claude Desktop** — it does not read your shell profile, so the same two values must go in the `"env"` block of `claude_desktop_config.json` (see [Claude Desktop setup](#claude-desktop)):

```json
"env": {
  "SCREENER_USERNAME": "your@email.com",
  "SCREENER_PASSWORD": "yourpassword"
}
```

Never commit real credentials — the values above are placeholders.

---

## Tools — 34 total

Grouped by category. See [Example workflows](#example-workflows) for the ones you'll reach for most.

### Company Research

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `search_company` | Find company by name or symbol | No |
| `get_company_overview` | Key ratios, price, 52W range, NSE/BSE codes, about, Screener's pros/cons | No |
| `get_financials` | P&L (TTM separate) / Balance Sheet / Cash Flow / Ratios incl. year-end P/E, P/B, ROE, debt-to-equity | No |
| `get_quarterly_results` | Last 8 quarters of results | No |
| `get_shareholding_pattern` | 8-quarter holding trends per category, promoter-group detection, promoter pledge | No |
| `get_peer_comparison` | Sector peer comparison table | No |
| `compare_companies` | 2–6 stocks side by side, with point-in-time red flags; interactive dashboard (MCP App) where supported, JSON everywhere | No |
| `get_full_analysis` | All data combined for deep analysis (same windows as the standalone tools) | No |
| `analyze_red_flags` | Rule-based red-flag checks over the full history, each with evidence and thresholds | No |

### Stock Screening

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `screen_stocks` | Screener.in query plus technical clauses (52W distance, RSI, DMA, volume spike) | Fundamental clauses only |
| `get_52_week_low_candidates` | Quality filters + proximity to 52-week low, with overview fields per match | No |
| `get_relative_valuation` | P/E and ROCE vs the industry median for up to 20 stocks at once | No |
| `compare_to_sector` | Stock's return vs its sector index and Nifty 50, with a market-vs-company verdict | No |
| `screen_by_theme` | Pre-built thematic screens — every theme's criteria are in the tool description | Query themes only |

### Document Analysis

| Tool | What it does | Extra deps needed |
|------|-------------|:---:|
| `get_document_list` | List annual reports & earnings call transcripts | No |
| `ask_company_research` | Ask a question over a company's annual reports and earnings calls — all recent ones, one `doc_type`, or one document (`year` / `quarter` / `pdf_url`) | Yes |
| `search_market_commentary` | Search a question across multiple companies' already-indexed documents at once | Yes |

> `ask_company_research` and `search_market_commentary` build on the same cache — the former indexes a company's documents and searches them (one or many); the latter searches only what's *already* indexed across several symbols (good for "which of these companies mentioned Y?").

### Corporate Events

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `get_company_announcements` | NSE corporate announcements with category filter — incl. `credit_rating` (CRISIL/ICRA/CARE/India Ratings) and `esg_rating` | No |
| `get_bulk_deals` | NSE bulk deals by company (`symbol`), by investor (`name`), or both | No |
| `get_insider_trading` | SEBI PIT promoter/KMP/designated-person trade disclosures, no size threshold (both NSE PIT feeds merged) | No |

### Market & Research

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `get_recent_news` | Recent news headlines for a company (Google News), newest first | No |
| `get_analyst_targets` | Consensus target price (mean/median/high/low, analyst count, rating split) + broker targets in recent headlines | No |
| `get_forward_outlook` | Analyst EPS/revenue estimates, order wins and capex filings, management guidance from the latest earnings call | No (`[ai]` for guidance) |
| `get_moat_signals` | Revenue share/rank and industry concentration (HHI), plus ROCE, margin and promoter-holding durability | No |
| `get_commodity_prices` | International benchmark price, period moves, ≈INR price + exposed companies' symbols (incl. wheat; tobacco and wood pulp as exposure-only) | No |
| `notebook_ai` | Save, read, and AI-summarize research notes locally | No |

### Portfolio

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `add_portfolio_stock` | Add/merge a holding into your local portfolio (quantity-weighted avg cost) | No |
| `update_portfolio_stock` | Overwrite quantity/avg price on an existing holding (partial sell, cost correction) | No |
| `remove_portfolio_stock` | Remove a holding entirely | No |
| `get_portfolio` | View holdings with live price, P&L (₹ and %), and weight | No |

> Stored locally at `~/.screener-mcp/portfolio.json` — no account, no external service, nothing leaves your machine.

### Live prices (FYERS, optional)

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `get_live_quote` | Live price, day change, OHLC, volume and quote time for up to 50 NSE symbols | FYERS |
| `fyers_status` | Whether FYERS is signed in today, until when, and the sign-in link | FYERS |

> With FYERS connected, `get_portfolio` and `get_company_overview` also use live prices. See [Live prices from FYERS](#live-prices-from-fyers).

### Telegram channels (optional)

| Tool | What it does | Login needed |
|------|-------------|:---:|
| `telegram_status` | Whether the channel reader is set up, which channels and topics are followed, posts stored, last pull, errors | Telegram |
| `get_channel_mentions` | Stocks your followed channels discussed: calls with entry, stop, targets and timeframe as written, every later update (target/stop hit, exit, cancel), and plain mentions, each with the post text and a link | Telegram |

> Reads only channels you have joined, with your own account, and never posts. Posts are stored on your machine. See [Telegram channels](#telegram-channels).

---

## Document analysis

Document analysis (`[ai]` extra) uses a local RAG pipeline:

```
ask_company_research("TCS", "What are the key risks?", year=2024)

  1. Fetch PDF link from Screener.in / NSE
  2. Download and parse with pdfplumber — two-column pages are split at the
     gutter, rotated/mirrored decorative text is dropped
  3. Chunk into 500-word overlapping segments
  4. Embed with sentence-transformers (runs locally, no API key needed)
  5. Store in ChromaDB (~/.screener-mcp/chroma_db/)
  6. Semantic search, re-ranked: question keywords boosted, BRSR boilerplate
     demoted (unless the question is about ESG), one hit per ~3-page window
  7. Return the top excerpts, each trimmed to ~900 characters around the
     question's terms, with document and page
```

Results are cached — the same report isn't re-downloaded or re-processed. Indexes built by an older version of the pipeline are rebuilt automatically on next use (from the cached PDF). Each document's `freshness` reports `last_indexed_at`, the PDF's `content_sha256`, its `etag` / `last_modified`, and `source_changed` (a HEAD check against the live PDF; `null` when the server gives no validators). If a report was revised or refiled, pass `force_reindex=True` to rebuild the index. The manifest lives at `~/.screener-mcp/index_manifest.json`.

---

## Stock screening

### Pre-built themes

Query themes run a Screener query across the whole market:

```
undervalued_small_cap       Small caps < ₹5000 Cr, ROCE > 15%, low debt, P/E < 20
high_roce_low_debt          ROCE > 20%, debt to equity < 0.3
compounders                 15%+ growth: revenue, profit, ROE, ROCE
turnaround                  Strong recent profit recovery
rising_profit_falling_price Profit up 15%+/yr over 3 years, price down over 1 year, P/E < 15
improving_roce              ROCE > 15% and above last year's and its 5-year average
hidden_gems                 Small cap, high ROCE, strong growth
dividend_aristocrats        Yield > 2%, dividend paid in each of the last 2 years, 3y payout > 20%
qarp                        Quality at reasonable price
micro_cap_growth            High-growth micro caps < ₹1000 Cr
```

Sector themes start from the companies actually in the sector — Screener's query language has no industry field — then filter them:

```
defense                     Nifty India Defence + Aerospace & Defense and Shipbuilding industries
ev_theme                    Nifty EV & New Age Automotive constituents
chemicals                   Specialty Chemicals industry + Nifty Chemicals
railways                    Railway Wagons industry + a curated list of railway PSUs/suppliers
renewable_energy            Curated list of wind/solar makers and green power producers
```

Every result says which universe it came from (`data.universe`), and `screen_by_theme`'s description carries each theme's exact criteria. Both `screen_by_theme` and `screen_stocks` take `page` to go past the first page of results.

### Custom screen syntax

```
Market Capitalization < 5000 AND Return on capital employed > 15 AND Debt to equity < 0.5
Profit growth 5Years > 20 AND Sales growth 5Years > 15 AND Debt to equity < 0.3
Dividend yield > 3 AND Return on equity > 15 AND Pledged percentage < 5
```

Supported operators: `>` `<` `>=` `<=` `=`, combined with `AND`, `OR` and parentheses

Full field list in [CONTRIBUTING.md](CONTRIBUTING.md#screenerinscreenerinquery-field-names).

### Technical clauses

Mix these with fundamental clauses using `AND`, `OR` and parentheses:

```
52 week low distance < 10        % above the 52-week low
52 week high distance > 30       % below the 52-week high
RSI < 30                         14-day RSI
Price above 200 DMA              also below / 20, 50, 200 DMA / "50 DMA above 200 DMA"
Price vs 50 DMA < -5             % above (+) or below (−) a moving average
Volume vs 20 day average > 2     today's volume ÷ 20-day average volume
```

```
Return on capital employed > 15 AND Debt to equity < 0.5 AND 52 week low distance < 10
RSI < 30 AND Price above 200 DMA                          (technical-only: no login needed)
(Return on capital employed > 20 OR Return on equity > 25) AND Price above 200 DMA
Return on capital employed > 15 AND (RSI < 30 OR 52 week low distance < 5)
```

Fundamental clauses run on Screener.in as usual. The technical clauses are then evaluated against up to `max_candidates` (default 150) of those matches, using daily price history from Screener's public chart API. A query with only technical clauses scans an NSE index instead (`universe`, default `nifty500`; also `nifty50`, `midcap100`, `smallcap100`, `smallcap250`, `bank`, `it`, `auto`, `pharma`, `fmcg`, `metal`, `realty`, `energy`, `defence`, `chemicals`, ...). If some candidates weren't checked, the response has `partial: true` and a `reason`. The 52-week range is based on closing prices.

When `OR` groups contain no technical clauses, they go to Screener unchanged, since Screener supports them. When a technical clause sits under an `OR`, each alternative runs as its own screen, up to 6. The results are merged, and each result's `matched_groups` shows which alternatives it satisfied.

### Result hygiene

Screens sorted by growth used to fill up with tiny illiquid names showing one-off numbers. Two guards are now on by default:

- **`min_market_cap`** (₹100 Cr) is added to the Screener query unless your query already has a `Market Capitalization` clause. Set it to `0` to turn it off.
- **`exclude_flagged`** drops rows with implausible numbers and lists them under `excluded_for_data_quality`. The checks are: P/E below 1, a quarterly profit jump over 500% on a small base, quarterly profit above sales, negligible sales, a price below ₹1, and a dividend yield above 25% (a special dividend or stale price). Set it to `False` to keep these rows, marked with `data_quality_flags`.

Pass `peer_relative=True` to add each result's P/E and ROCE compared with its industry median.

---

## Keeping it working

Screener.in changes its pages without notice. A scheduled GitHub Action ([`canary.yml`](.github/workflows/canary.yml)) runs [`scripts/canary.py`](scripts/canary.py) every day. It checks the real tools against known companies: overviews, the standalone fallback, financial tables, symbol resolution, a technical screen, sector comparison and peers. If a check fails, it opens a **"Live canary failing"** issue. NSE checks only warn, because NSE often blocks GitHub's IPs. You can run it locally with `python scripts/canary.py`.

---

## Response envelope

Every tool returns the same shape:

```json
{
  "status": "ok | partial | error",
  "partial": false,
  "warnings": ["MSUMI publishes no consolidated financials — showing standalone figures instead."],
  "data": { "...": "..." },
  "missing_fields": ["pe"],
  "reason": "Screener.in's page had no value for these fields.",
  "meta": { "symbol": "MSUMI", "financial_type": "standalone", "requested_symbol": "MOTHERSONWIR", "interpreted_as": "MSUMI" },
  "error": { "type": "symbol_ambiguous", "message": "...", "candidates": [{ "symbol": "TMPV", "name": "Tata Motors Passenger Vehicles Ltd" }] }
}
```

- **Blank source fields** show up in `missing_fields` with `status: "partial"`, and their values are `null`, never `""` or `0`.
- **Companies without subsidiaries** have a blank `/consolidated/` page on Screener. The server falls back to standalone figures and says so in `warnings`.
- **Non-canonical symbols** like `MOTHERSONWIR` resolve through Screener search. A confident match proceeds and is recorded in `meta.interpreted_as`. An ambiguous one returns `error.candidates` (top 3), so you can retry in one call.
- **Errors are flagged on the wire.** When `status` is `"error"`, the MCP result also has `isError: true`, so clients treat the call as failed rather than as an empty success.
- **Slow tools report progress.** Screens, full analyses, forward outlook and document search send a progress notification every few seconds (when the client asks for progress), so a long call never looks hung.
- **Implausible ratio history**, such as days-based metrics out of range or a Cash Conversion Cycle that doesn't equal debtor + inventory − payable days, is kept but marked `data_quality_flag: true`, with a reason.

---

## Remote HTTP server

By default this runs over stdio — a local process, used by `claude mcp add`, Claude Desktop, and similar clients. Some integrations — any client that asks for an HTTPS **Server URL** — instead need a network server.

Run it with HTTP transport:

```bash
MCP_TRANSPORT=streamable-http PORT=8000 python run_server.py
# Serves MCP over HTTP at http://<host>:8000/mcp
```

Env vars:

| Variable | Purpose | Default |
|----------|---------|---------|
| `SCREENER_USERNAME` | Screener.in login email (needed for screening tools) | — |
| `SCREENER_PASSWORD` | Screener.in password | — |
| `MCP_TRANSPORT` | `stdio` or `streamable-http` | `stdio` |
| `PORT` / `MCP_PORT` | Port to listen on (HTTP transport only) | `8000` |
| `MCP_HOST` | Bind address (HTTP transport only) | `127.0.0.1` (the Docker image sets `0.0.0.0`) |
| `MCP_PUBLIC_URL` | Public `https://` address of the server, without `/mcp` (HTTP only). Setting it requires sign-in | — |
| `MCP_AUTH_PASSPHRASE` | Turns on OAuth sign-in for the HTTP transport; at least 12 characters | — (no sign-in) |
| `FYERS_APP_ID` / `FYERS_SECRET_KEY` | Your FYERS data-only API app, for live prices (needs sign-in and `MCP_PUBLIC_URL`) | — (no live prices) |
| `CHROMA_PERSIST_DIR` | Where the document-analysis vector store is cached | `~/.screener-mcp/chroma_db` |

To get a public HTTPS URL, deploy this to any host that can run a long-lived Python process and terminate TLS for you (Render, Railway, Fly.io, a VM behind a reverse proxy, etc.), then point the client at `https://your-host/mcp`.

> Without `MCP_AUTH_PASSPHRASE`, a `streamable-http` server has no authentication. Don't expose it to the internet like that, especially if you set `SCREENER_USERNAME`/`PASSWORD`, since anyone who can reach the URL would act as your Screener.in account. Turn on sign-in as described below, or put it behind your platform's access controls.

### Use from claude.ai (web and mobile)

claude.ai custom connectors also appear in the Claude mobile apps. They connect over HTTPS and sign in with OAuth, so the server includes a small single-user OAuth sign-in: set `MCP_AUTH_PASSPHRASE` and `MCP_PUBLIC_URL`, and the first time you connect, a page asks for your passphrase. Clients then get 1-hour access tokens with rotating 30-day refresh tokens. Registered clients and *hashed* tokens are kept in `~/.screener-mcp/oauth.json`, so a restart doesn't sign you out. Five wrong passphrases lock sign-in for 15 minutes.

**Running it on your own computer is the recommended setup.** NSE blocks most cloud IP addresses, so announcements, bulk deals and insider trades usually fail from a hosted server but work from a home connection. Your portfolio and notes also stay on your machine. The trade-off is that the connector is offline whenever the computer is off or asleep.

Steps on macOS (other systems are similar):

1. **Install the server** as a persistent command (from this repository, since the sign-in is not on PyPI yet):
   ```bash
   uv tool install 'git+https://github.com/theinvincibles-ai/screener-mcp'
   ```
2. **Get a public HTTPS address.** With [Tailscale](https://tailscale.com/kb/1223/funnel) (free, no domain needed), install it, sign in, then run `tailscale funnel --bg 8000`. It prints an address like `https://your-mac.tailnet-name.ts.net`. [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) works too if you have a domain on Cloudflare.
3. **Store your secrets in the Keychain.** Each command prompts for the value. The Screener.in pair is optional:
   ```bash
   security add-generic-password -U -s screener-mcp -a auth-passphrase -w
   security add-generic-password -U -s screener-mcp -a screener-username -w
   security add-generic-password -U -s screener-mcp -a screener-password -w
   ```
4. **Run it in the background.** Copy [`scripts/macos/screener-http.sh`](scripts/macos/screener-http.sh) somewhere permanent, and copy [`scripts/macos/com.screener-mcp.http.plist`](scripts/macos/com.screener-mcp.http.plist) to `~/Library/LaunchAgents/`. Fill in its two `REPLACE_ME` values (the script path and your public address), then run:
   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.screener-mcp.http.plist
   ```
   It starts at login and restarts if it stops. Logs go to `/tmp/screener-mcp-http.log`.
5. **Keep the Mac awake** on power: **System Settings → Battery → Options → Prevent automatic sleeping on power adapter when the display is off** (on a desktop Mac, the same option is under **Energy**). With the lid closed, a MacBook sleeps unless an external display is connected.
6. **Add the connector** on claude.ai: **Settings → Connectors → Add custom connector**, paste `https://your-address/mcp` and click **Connect**. Sign in with your passphrase. It then shows up in the Claude mobile app too.

To sign out every client, stop the server, delete `~/.screener-mcp/oauth.json` and start it again.

### Live prices from FYERS

If you have a [FYERS](https://fyers.in) account, the server can use live quotes from it instead of Screener's delayed price. It only calls FYERS's quote endpoints, and the app you create can't place orders. This needs the claude.ai setup above, because FYERS sends you back to your server's public address after you sign in.

1. **Create a data-only app** in the [FYERS API dashboard](https://fyers.in/web/api-dashboard/user-apps): **Create non-trading app**, any name, and set the **Redirect URL** to `https://your-address/fyers/callback`.
2. **Store the App ID and secret** in the Keychain (each command prompts for the value), then restart the service:
   ```bash
   security add-generic-password -U -s screener-mcp -a fyers-app-id -w
   security add-generic-password -U -s screener-mcp -a fyers-secret -w
   launchctl kickstart -k gui/$(id -u)/com.screener-mcp.http
   ```
3. **Sign in once a day** at `https://your-address/fyers/login`. FYERS tokens expire every day at 6:30 AM IST. The page asks for your connector passphrase, then sends you to FYERS's own login, so your FYERS password never reaches the server. When you're not signed in, tools say so and give this link, and fall back to Screener's delayed price.

Today's token is kept in `~/.screener-mcp/fyers_token.json`, readable only by you. FYERS's free plan allows 5 data requests a second and 5,000 a day, with up to 50 symbols per quote request. The server batches symbols and reuses a quote for 5 seconds to stay well under that.

### Telegram channels

The server can read Telegram channels you have joined (private ones and forum topics included) and pick out the stocks they discuss, with the entry, stop loss, targets and timeframe each post gives, and every follow-up such as "T1 done", "SL hit", "exit" or "cancel". It signs in as you, reads only, and never posts, joins or marks anything as read. It is a research aid: it reports what the channel said, not whether to trade.

1. **Create a Telegram app** at [my.telegram.org](https://my.telegram.org) → **API development tools**, to get an `api_id` and `api_hash`.
2. **Store them in the Keychain** (each command prompts for the value):
   ```bash
   security add-generic-password -U -s screener-mcp -a telegram-api-id -w
   security add-generic-password -U -s screener-mcp -a telegram-api-hash -w
   ```
3. **Install Telegram support** and restart the service:
   ```bash
   uv tool install --force 'screener-mcp[telegram] @ git+https://github.com/theinvincibles-ai/screener-mcp'
   launchctl kickstart -k gui/$(id -u)/com.screener-mcp.http
   ```
4. **Sign in once** with [`scripts/macos/telegram.sh`](scripts/macos/telegram.sh), which reads the keys from the Keychain. Telegram sends a code to your Telegram app; type it (and your two-step password, if set) into Terminal only:
   ```bash
   ./telegram.sh login
   ./telegram.sh channels                    # your channels, groups and forum topics, with ids
   ./telegram.sh follow 1234567890            # every post in that channel
   ./telegram.sh follow 2233445566 --topic 12 --topic 15   # only some topics of a forum group
   ```

The server pulls new posts every 15 minutes during market hours (8:45 AM to 4 PM IST, weekdays) and hourly otherwise, plus on demand when a tool is called. The first pull goes back 30 days. It re-reads the last 48 hours for edits and deletions: a deleted post is kept and marked, so a losing call removed later still counts.

Everything stays in `~/.screener-mcp/telegram/` (owner-only): the sign-in session (`telegram.session`, which acts as your account, so keep it private), the followed channels, the posts (`messages.db`) and a weekly copy of NSE's symbol list used to spot tickers. Logs carry counts and ids, never message text or keys. To stop, run `./telegram.sh logout`, or end the session from Telegram's **Settings → Devices**.

Stocks are found by NSE symbol (`#BEL`, `$BEL`, `NSE:BEL` or a capitalised `BEL`), or by company name. Add your own nicknames in `~/.screener-mcp/telegram/aliases.json`, e.g. `{"mazagon": "MAZDOCK"}`. Chart images and screenshots aren't read yet; their captions are.

---

## Docker

A `Dockerfile` is included. It installs the full `[ai]` extras (document analysis included) — expect a slow first build (~1-2GB with torch).

```bash
# Build and tag the image
docker build -t screener-mcp:latest .

# Run it, exposing the HTTP port and setting credentials
docker run -p 8000:9000 \
  -e PORT=9000 \
  -e SCREENER_USERNAME=you@example.com \
  -e SCREENER_PASSWORD=yourpassword \
  screener-mcp:latest
```

Then point the client at `http://<host>:8000/mcp`.

---

## Data sources & limitations

| Source | Data provided |
|--------|--------------|
| [Screener.in](https://www.screener.in) | 10+ years of financials, ratios, shareholding, peers |
| [NSE India](https://www.nseindia.com) | Announcements, annual reports, bulk deals, insider trading disclosures |
| [Yahoo Finance](https://finance.yahoo.com) | International commodity benchmarks (COMEX, ICE Brent, NYMEX), USD/INR, analyst consensus targets |
| [Google News](https://news.google.com) RSS | Recent headlines, including broker target-price mentions |

- Financial data lags by ~1 quarter
- Screener.in rate-limits bursts. The client paces requests (`SCREENER_MIN_INTERVAL`, default 0.25s) and caps concurrency (`SCREENER_MAX_CONCURRENCY`, default 3). Any 429 pauses all requests for a shared cooldown, and requests are then retried. Price history is cached in `~/.screener-mcp/price_cache`: during market hours for 15 minutes, otherwise until the next session. A cold technical screen can take a minute; repeat screens are fast. Set `SCREENER_PRICE_CACHE=0` to disable the cache
- Commodity prices are the international benchmarks MCX contracts track. The INR figure is a plain FX conversion, before import duty and GST, so it's below the MCX quote. Nickel has no free feed and returns `partial`
- `get_company_overview` returns `data.price_freshness`: `price_as_of`, whether the price is an intraday print or the last close, the previous close and the day's change. A price older than a few days is flagged as stale
- Analyst targets come from two sources that cover different brokers and dates, so they won't match: Yahoo Finance's consensus and targets extracted from recent headlines. Treat either as one view, not the market's
- For banks, NBFCs and insurers, debt-to-equity and working-capital-day checks are skipped because they aren't meaningful for lenders. Judge these companies on ROE, asset quality and capital adequacy
- Document analysis requires machine-readable PDFs (scanned/image-only PDFs may fail)
- NSE bulk deals only capture single trades > 0.5% of equity
- The NSE-backed tools (`get_company_announcements`, `get_insider_trading`, `get_bulk_deals`) depend on NSE's public API, which often rate-limits or blocks server IPs. When that happens the tool returns `status: "error"` with `error.type: "upstream_unavailable"`. When only some bulk-deal days fail, it returns `partial`. An empty list with `status: "ok"` means NSE really had no rows. These tools resolve fuzzy symbols the same way as the Screener tools, and they return a `not_on_nse` error for companies listed only on BSE.
- This is a research tool — not financial advice

---

## Project layout

```
screener-mcp/
├── run_server.py
├── scripts/canary.py               # Daily live check against Screener.in (see .github/workflows/canary.yml)
├── scripts/macos/                  # launchd service + Keychain start scripts (HTTP server, Telegram CLI)
├── tests/                          # Offline tests (no network) + a real-page fixture
└── src/screener_mcp/
    ├── server.py                   # MCPServer (MCP SDK v2) — all 34 tool definitions
    ├── telegram_cli.py             # screener-mcp-telegram: sign-in, list and follow channels
    ├── client.py                   # Screener.in HTTP client + auth
    ├── core/
    │   ├── envelope.py             # Standard response envelope for every tool
    │   ├── company_page.py         # Symbol resolution + page fetch + standalone fallback
    │   ├── quality.py              # Missing-field detection + ratio sanity bounds
    │   ├── technicals.py           # Price history → 52W range, DMA, RSI, volume ratio
    │   ├── indices.py              # NSE index universes + sector benchmarks
    │   ├── yahoo.py                # Yahoo Finance session (consensus targets, estimates)
    │   ├── industry.py             # Industry pages → medians, revenue share, HHI
    │   ├── nse_client.py           # NSE India API (announcements, filings)
    │   ├── oauth.py                # Single-user OAuth sign-in for the HTTP transport
    │   ├── history.py              # Year-by-year series, computed ROE / debt-to-equity, CAGR
    │   ├── valuation_history.py    # Year-end P/E and P/B from Screener's chart API
    │   ├── telegram_client.py      # Read-only Telegram poller (Telethon, optional)
    │   ├── telegram_store.py       # Local SQLite store for posts, edits, deletions
    │   ├── telegram_extract.py     # Stocks, levels, timeframe and call events from posts
    │   ├── telegram_symbols.py     # NSE symbol list for ticker matching
    │   ├── rag.py                  # PDF → chunk → embed → query pipeline
    │   └── vector_store.py         # ChromaDB wrapper
    ├── parsers/
    │   ├── company.py              # Screener.in company page parser
    │   └── screener.py             # Screen results parser
    ├── ui/
    │   ├── stock_comparison.html   # compare_companies dashboard (MCP App)
    │   └── ext-apps-app-with-deps.js  # vendored MCP Apps runtime (MIT)
    └── tools/
        ├── company_tools.py        # Company data tools
        ├── screening_tools.py      # Stock screening + themes
        ├── technical_tools.py      # Technical screens, 52W-low candidates, sector-relative
        ├── analysis_tools.py       # Full analysis, rule-based red flags
        ├── documents.py            # Annual reports + earnings calls (RAG)
        ├── announcements.py        # NSE corporate announcements (incl. credit/ESG ratings)
        ├── shareholders.py         # NSE bulk deals (by company and/or investor)
        ├── insider_trading.py      # SEBI PIT insider trading disclosures
        ├── market_tools.py         # Recent news + analyst targets
        ├── research_tools.py       # Relative valuation, moat signals, forward outlook
        ├── commodities.py          # Commodity price analysis
        ├── notebook.py             # Research notes
        ├── portfolio.py            # Local portfolio tracker
        └── telegram_tools.py       # Telegram status + channel mentions
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — adding a new tool takes ~10 minutes.

```bash
git clone https://github.com/LogeshR15/screener-mcp
cd screener-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The test suite is offline — no network, no Screener.in credentials — and checks that the tool registry and the docs still agree with each other. If you add or rename a tool, tests fail until you update `EXPECTED_TOOLS` in [`tests/test_tools.py`](tests/test_tools.py), the `server.py` docstring, and the README tool table.

**Dependency files:**
- `pyproject.toml` — the source of truth; `[ai]` extra adds document analysis
- `requirements.txt` — full install (core + document analysis, pulls in torch)
- `requirements-core.txt` — lightweight install, no document-analysis tools

---

## License

[MIT](LICENSE) © Logesh Ramasamy

---

## Contact

**Logesh Ramasamy** · logeshl2003@gmail.com · [LinkedIn](https://linkedin.com/in/logesh-ramasamy/)
