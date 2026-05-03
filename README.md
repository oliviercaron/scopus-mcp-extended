# Scopus MCP — Extended

A comprehensive **Model Context Protocol (MCP)** server for the Elsevier Scopus, ScienceDirect, PlumX and Embase APIs. Lets an LLM (Claude, ChatGPT, Cursor, …) search the academic literature, retrieve abstracts, citations, references, full article text, figures, altmetrics, and more — all through your own institutional Elsevier subscription.

## Features

- **25 tools** spanning Scopus Search, Author/Affiliation APIs, ScienceDirect Article Retrieval, PlumX altmetrics, Embase, Holdings — and convenience composers (BibTeX export, co-author networks).
- **Institutional token (`X-ELS-Insttoken`) support** out of the box. Required by Elsevier for off-campus access to Author Search, Affiliation APIs, Article Retrieval, etc.
- **Hardened binary downloads** via `download_object`: SSRF-protected URL validation (canonical `urlparse` checks, no redirect follow, percent-encoded traversal rejected); sandboxed file writes with exclusive-create; streaming with size cap (env-tunable, default 100 MB).
- **Clear error surfacing**: when Scopus returns 401, the tool raises `ScopusAuthError` (credentials not recognized — typically off-campus + no insttoken); 403 raises `ScopusSubscriptionError` (credentials valid, resource not in your contract). Both subclass `ScopusAccessError` so existing catches still work. The actual Elsevier `statusCode` + `statusText` are exposed both in the message and as structured attributes on the exception.
- **Per-class HTTP cache** with sensible TTLs (search 1 h, abstract 30 d, author 7 d), automatic retry/backoff on 5xx, request quota tracking.
- **Access discovery built in**: `check_article_access` and `get_quota_status` let you probe what your specific subscription gives you before bulk operations.

## Tool inventory (25 total)

Endpoint access depends on your specific Elsevier subscription. See the [Endpoint access notes](#endpoint-access-notes) section below for what worked / didn't work in our development testing.

### Search
| Tool | Description |
|---|---|
| `search_scopus` | Full-text search of the Scopus index. Supports field-prefix syntax (TITLE-ABS-KEY, AUTH, AFFIL, AU-ID, REFEID...). `view='COMPLETE'` opt-in for full author lists. |
| `search_authors` | Search authors by name or affiliation. |
| `search_affiliations` | Search institutions. |
| `search_journals` | Search journals by title with SJR/SNIP/CiteScore metrics. |
| `search_sciencedirect` | Full-text search of ScienceDirect — indexes the article body, not just bibliographic metadata. |

### Retrieve metadata
| Tool | Description |
|---|---|
| `get_abstract_details` | Bibliographic record + abstract by Scopus ID. |
| `get_abstract_by_doi` | Same, by DOI. |
| `get_abstract_references` | Reference list (works cited BY this paper). Paginates internally up to `max_refs`. |
| `get_author_profile` | Author profile: h-index, citation counts, current affiliation. |
| `get_affiliation` | Institution profile by Scopus Affiliation ID. |
| `get_journal_by_issn` | Journal metrics (SJR, SNIP, CiteScore) by ISSN. |

### ScienceDirect article access
| Tool | Description |
|---|---|
| `get_article` | Full article from ScienceDirect — `view='META_ABS'` for metadata + abstract, `view='FULL'` for the article body (when entitled). |
| `get_objects` | List embedded objects of an article: figures, tables, supplementary files, with download URLs in multiple mime types. |
| `download_object` | Download a binary object (figure, table image, supplementary file) to a sandboxed local folder. SSRF-protected. |
| `check_article_access` | Quickly answer "do I have full-text access to this DOI?" before bulk downloading. Returns ENTITLED / OPEN_ACCESS / NOT_ENTITLED / NOT_FOUND. |
| `get_article_entitlement` | Lower-level entitlement check, returns the raw Elsevier response. |

### Altmetrics
| Tool | Description |
|---|---|
| `get_plumx_metrics` | PlumX altmetrics — Twitter mentions, news, blog posts, downloads, Mendeley readers, citations. Much broader impact picture than just citation counts. |

### Forward citations
| Tool | Description |
|---|---|
| `get_citing_papers` | Papers that CITE a given paper (uses a REFEID query). |
| `get_citations_overview` | Year-by-year citation history. |
| `get_citation_count` | Bare citation count for one or more documents (lightweight, batch). |

### Holdings & subscription discovery
| Tool | Description |
|---|---|
| `get_holdings_report` | Lists journals your institution is entitled to access. |

### Embase
| Tool | Description |
|---|---|
| `get_embase_record` | Retrieve a record from Embase. |

### Composition / utility (no new endpoint)
| Tool | Description |
|---|---|
| `get_bibtex` | Generate a BibTeX entry from a DOI (with proper LaTeX escaping). |
| `get_author_coauthors` | List the most frequent co-authors of an author, ranked by shared paper count. |
| `get_quota_status` | Current API quota (limit / remaining / reset epoch). |

## Quickstart

### 1. Get an Elsevier API key (and optionally an Insttoken)

1. Apply at the [Elsevier Developer Portal](https://dev.elsevier.com/) using your institutional email (free public emails are usually rejected).
2. **Strongly recommended**: also request an *Institutional Token* from Elsevier support. From [Elsevier's auth doc](https://dev.elsevier.com/tecdoc_api_authentication.html): *"An insttoken is an additional security token submitted in tandem with your APIKey. […] The insttoken represents full access to a customer account within our authentication and entitlements system."* In our development testing, several endpoints (Author Search, Affiliation Search, Author/Affiliation Retrieval) returned 401 without the insttoken and 200 with it — your mileage may vary. Insttokens are issued by Elsevier support to existing institutional subscribers; ask your library or contact Elsevier directly.

### 2. Install

```bash
git clone https://github.com/oliviercaron/scopus-mcp-extended.git
cd scopus-mcp-extended
pip install -e .   # or: uv pip install -e .
```

Or directly from this repo without cloning:

```bash
pip install git+https://github.com/oliviercaron/scopus-mcp-extended.git
```

### 3. Configure your MCP client

The MCP server reads two env vars: `SCOPUS_API_KEY` (required) and `SCOPUS_INST_TOKEN` (recommended). Below are configs for all major MCP-compatible clients.

#### Claude Code (Anthropic CLI)

Use the `claude mcp add` command (recommended):

```bash
claude mcp add scopus-mcp \
  --env SCOPUS_API_KEY=your-api-key-here \
  --env SCOPUS_INST_TOKEN=your-insttoken-here \
  -- scopus-mcp
```

Or edit `~/.claude.json` directly under the `mcpServers` key:

```json
{
  "mcpServers": {
    "scopus-mcp": {
      "command": "scopus-mcp",
      "env": {
        "SCOPUS_API_KEY": "your-api-key-here",
        "SCOPUS_INST_TOKEN": "your-insttoken-here"
      }
    }
  }
}
```

#### Claude Desktop

Edit `claude_desktop_config.json`:
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "scopus-mcp": {
      "command": "scopus-mcp",
      "env": {
        "SCOPUS_API_KEY": "your-api-key-here",
        "SCOPUS_INST_TOKEN": "your-insttoken-here"
      }
    }
  }
}
```

#### Codex CLI (OpenAI)

Codex uses TOML, not JSON. Edit `~/.codex/config.toml`:

```toml
[mcp_servers.scopus-mcp]
command = "scopus-mcp"

[mcp_servers.scopus-mcp.env]
SCOPUS_API_KEY = "your-api-key-here"
SCOPUS_INST_TOKEN = "your-insttoken-here"
```

If your `scopus-mcp` is installed in a Python venv (rather than globally), you may need the absolute path to the venv's binary:

```toml
[mcp_servers.scopus-mcp]
command = "/path/to/venv/bin/scopus-mcp"
```

#### Cursor IDE

Edit `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project-scoped, takes precedence):

```json
{
  "mcpServers": {
    "scopus-mcp": {
      "command": "scopus-mcp",
      "env": {
        "SCOPUS_API_KEY": "your-api-key-here",
        "SCOPUS_INST_TOKEN": "your-insttoken-here"
      }
    }
  }
}
```

#### Other MCP-compatible clients (Antigravity, Cline, Continue, Windsurf, …)

Any client that implements the MCP standard accepts a server entry of the same JSON shape (`command` + `env`). Locate your client's MCP config file (often `mcp.json` or a section in the IDE settings) and add the same `scopus-mcp` block as the Claude Desktop / Cursor examples above.

#### Optional env vars

| Var | Default | Purpose |
|---|---|---|
| `SCOPUS_API_KEY` | (required) | Your Elsevier API key. |
| `SCOPUS_INST_TOKEN` | none | Institutional token. Unlocks Author / Affiliation / Article Retrieval APIs from off-campus. |
| `SCOPUS_DOWNLOAD_DIR` | `<tmp>/scopus_mcp_objects/` | Directory where `download_object` writes files. |
| `SCOPUS_DOWNLOAD_MAX_BYTES` | `104857600` (100 MB) | Max size per `download_object` call. |
| `CACHE_TTL_SEARCH` | `3600` (1 h) | TTL for search responses. |
| `CACHE_TTL_ABSTRACT` | `2592000` (30 d) | TTL for abstract retrieval responses. |
| `CACHE_TTL_AUTHOR` | `604800` (7 d) | TTL for author profile responses. |
| `CACHE_TTL_DEFAULT` | `86400` (1 d) | Fallback TTL. |

## Usage examples

### Quick: "find papers, check access, grab full text + figures"

```text
Find me 10 recent papers about social media influencer marketing in J Retailing & Consumer Services
→ search_scopus "TITLE-ABS-KEY(influencer marketing) AND SRCTITLE(retailing)" count=10

For each, check if I can get full text:
→ check_article_access(doi=...) for each result

For the ENTITLED ones, get the article body and figures:
→ get_article(doi=..., view="FULL")
→ get_objects(value=..., identifier="pii")
→ download_object(url=...) for each high-res JPEG figure

Build a BibTeX bibliography:
→ get_bibtex(doi=...) for each result

Show me the altmetrics impact for the top citation:
→ get_plumx_metrics(value=..., identifier="doi")
```

### "Map a research field"

```text
Find seminal papers on social contagion in marketing:
→ search_scopus "TITLE-ABS-KEY(social contagion AND marketing) AND PUBYEAR > 2010"

Who are the leading authors?
→ for each top paper: get_abstract_details → identify authors
→ get_author_profile(author_id=...) for each
→ get_author_coauthors(author_id=...) to map collaboration networks

What does each paper cite?
→ get_abstract_references(scopus_id=...)

What does the journal landscape look like?
→ search_journals(title="marketing")
→ get_journal_by_issn(issn="...") for SJR/SNIP/CiteScore
```

## Endpoint access notes

Elsevier organises access in three layers, each documented in different places:

### Layer 1 — The institutional token (`X-ELS-Insttoken`)

Verbatim from the [Scopus API Getting Started Guide v1, Sept 2023](https://dev.elsevier.com/guides/Scopus%20API%20Guide_V1_20230907.pdf), section 2.2:

> *"An institutional token, or insttoken, is an additional security token submitted in tandem with your API Key. Insttokens are only available for customers or partners working on behalf of a customer that cannot use IP authentication to access the Scopus APIs and thus, must be enabled manually by an Elsevier representative to use an API Key. An institutional token gives full access to the customer account within Elsevier authentication and entitlements system."*

Translation: with an insttoken you behave as if you were on your institution's IP range, anywhere. It is the recommended setup for off-campus development.

### Layer 2 — Access-controlled APIs / fields / views (Scopus)

Verbatim from the same guide, section 2.3:

> *"Access-controlled Scopus APIs and permissions that are available only upon request include:*
> - *Citation Overview API*
> - *refEID field*
> - *Index Keyword field*
> - *DOCUMENTS view of the Affiliation Retrieval and Author Retrieval APIs"*

This explains the two distinct error patterns you may hit:
- A whole API that's access-controlled returns **HTTP 403** with statusText *"Requestor configuration settings insufficient for access to this resource"* (e.g. Citation Overview).
- A field that's access-controlled inside an otherwise-allowed API returns **HTTP 400 INVALID_INPUT** with *"Use of certain field restrictions in the search query is not allowed for this requestor"* (this is what `REFEID(...)` queries trigger — the Search API endpoint is open, but the field is gated).

The same Elsevier page on [Default API Key Settings](https://dev.elsevier.com/api_key_settings.html) lists a broader cross-product set of access-controlled APIs:

> *"The access-controlled APIs include: Scopus Citation Overview, Scopus Author Feedback, ScienceDirect Full-Text Entitlement, ScienceDirect Article Hosting Permissions, ScienceDirect Holdings Report, Embase Search and Retrieval, Engineering Village Search and Retrieval, Pharmapendium API, SUSHI COP5 API."*

> *"Additionally, access to specialized APIs is not enabled by default, as use cases for specialized APIs require review from Elsevier's API Support team."*

To get any of these enabled, contact Elsevier support with your API Key + use case.

### Layer 3 — Product subscription (Scopus, ScienceDirect, Embase, …)

Even APIs that are not access-controlled require your institution to be subscribed to the relevant Elsevier product. From [dev.elsevier.com/about.html](https://dev.elsevier.com/about.html):

> *"Furthermore, full API access is only granted to clients that run within the networks of organizations that have subscriptions to the corresponding Elsevier product. Clients without subscriptions have access to limited basic metadata for most publications and citation records, as well as to basic search functionality."*

In particular:
- **PlumX Metrics** is bundled with Scopus, per the Scopus API Guide v1 section 12: *"All active Scopus subscriptions include access to the PlumX Metrics API"*.
- **Embase** is a [separate Elsevier product](https://www.elsevier.com/products/embase) with its own subscription tiers — having Scopus does NOT give you Embase.
- **ScienceDirect** is a separate product. Article Retrieval `view=FULL` requires the institution to be entitled to the specific article (Elsevier's standard publishing entitlement model).

### What we observed during development

Empirical results from testing every tool with one specific institutional API key + insttoken (Université Paris-Dauphine subscription, an academic Scopus + ScienceDirect subscriber). **Your access will differ** depending on your institution.

| Tool / endpoint | Result | Explained by |
|---|---|---|
| `search_scopus` (TITLE-ABS-KEY, AUTH, AU-ID, AFFIL, …) | ✅ 200 | Standard Scopus subscription |
| `search_scopus` with `REFEID(...)` | ❌ 400 INVALID_INPUT | `refEID field` is access-controlled (Layer 2) |
| `search_authors`, `search_affiliations` | ✅ 200 | Insttoken provides the institutional context |
| `get_author_profile`, `get_affiliation` | ✅ 200 | Same |
| `search_journals`, `get_journal_by_issn` | ✅ 200 | Serial Title API — Scopus standard |
| `get_abstract_details`, `get_abstract_by_doi`, `get_abstract_references` | ✅ 200 | Abstract Retrieval — Scopus standard |
| `search_sciencedirect`, `get_article`, `get_objects`, `download_object` | ✅ 200/300 | ScienceDirect subscription |
| `check_article_access` (uses `view=ENTITLED` on Article Retrieval) | ✅ 200 | View available within Article Retrieval |
| `get_plumx_metrics` | ✅ 200 | Bundled with active Scopus subscriptions |
| `get_quota_status` | ✅ Always | Reads cached `X-RateLimit-*` headers |
| `get_citations_overview` | ❌ 403 AUTHENTICATION_ERROR | Citation Overview API is access-controlled (Layer 2) |
| `get_citation_count` | ❌ 403 (same) | Falls under access-controlled Scopus APIs in our subscription |
| `get_article_entitlement` (raw endpoint, not the view) | ❌ 403 (same) | "ScienceDirect Full-Text Entitlement" is access-controlled (Layer 2) |
| `get_holdings_report` | ❌ 403 (same) | "ScienceDirect Holdings Report" is access-controlled (Layer 2) |
| `get_embase_record` | ❌ 403 (same) | Embase is a separate product (Layer 3) — Dauphine has no Embase subscription |
| `get_citing_papers` | ❌ 400 (same as REFEID) | Composes `REFEID(...)` query, gated by access-controlled field |

### Practical advice

- **Probe access by trying a tool**. HTTP 401 raises `ScopusAuthError` (credentials not recognized — likely missing insttoken); HTTP 403 raises `ScopusSubscriptionError` (resource not in your subscription, must be enabled per-key by Elsevier support). HTTP 400 (e.g. on `REFEID(...)` queries) propagates the raw `httpx.HTTPStatusError` because the field-level access control surfaces as INVALID_INPUT rather than 401/403. All three carry the Elsevier `statusCode` + `statusText` for diagnostics.
- **Use `check_article_access(doi)` before bulk full-text downloads** — it's the cheapest way to filter a DOI list to what you can actually retrieve.
- **Request an insttoken from Elsevier support** if you'll work off-campus. In our tests, Author/Affiliation/Article Retrieval APIs returned 401 without it and 200 with it.
- **Need a normally access-controlled API?** Email Elsevier support with your API Key + use case. Per the Scopus API Guide: *"Please note that our policy is to enable special access on only one API Key per project. Quotas are then adjusted accordingly to meet the needs of that project."*

### Default rate limits (from the official Scopus API Guide v1, Sept 2023)

The Scopus APIs have per-key throttling levels (Development / Low / Medium / High) and weekly quotas. Defaults from page 11:

| API | Throttle (req/s) Dev / Low / Med / High | Weekly quota |
|---|:---:|:---:|
| Abstract Retrieval | 9 / 9 / 12 / 15 | 10,000 |
| Abstract Citation Count | 15 / 15 / 20 / 20 | 20,000 |
| Serial Title | 6 / 6 / 9 / 12 | 20,000 |
| Subject Classifications | N/A | N/A |
| Affiliation Retrieval | 9 / 9 / 12 / 15 | 5,000 |
| Author Retrieval | 6 / 6 / 9 / 12 | 5,000 |
| Affiliation Search | 6 / 6 / 9 / 12 | 5,000 |
| Author Search | 6 / 6 / 9 / 12 | 5,000 |
| Scopus Search | 9 / 9 / 12 / 15 | 20,000 |

`get_quota_status` returns the live values from the `X-RateLimit-*` response headers.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

PRs welcome.

If you want to add a new Elsevier endpoint, follow the existing pattern:
1. Add a client method in `src/scopus_mcp/client.py`
2. Add a parser in `src/scopus_mcp/utils.py` (named `clean_<endpoint>`)
3. Register the tool in `src/scopus_mcp/server.py` (both `list_tools` and `call_tool` handlers)
4. Add a test in `tests/`
