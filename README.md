# Scopus MCP — Extended

A comprehensive **Model Context Protocol (MCP)** server for the Elsevier Scopus, ScienceDirect, PlumX and Embase APIs. Lets an LLM (Claude, ChatGPT, Cursor, …) search the academic literature, retrieve abstracts, citations, references, full article text, figures, altmetrics, and more — all through your own institutional Elsevier subscription.

## Features

- **25 tools** spanning Scopus Search, Author/Affiliation APIs, ScienceDirect Article Retrieval, PlumX altmetrics, Embase, Holdings — and convenience composers (BibTeX export, co-author networks).
- **Institutional token (`X-ELS-Insttoken`) support** out of the box. Required by Elsevier for off-campus access to Author Search, Affiliation APIs, Article Retrieval, etc.
- **Hardened binary downloads** via `download_object`: SSRF-protected URL validation (canonical `urlparse` checks, no redirect follow, percent-encoded traversal rejected); sandboxed file writes with exclusive-create; streaming with size cap (env-tunable, default 100 MB).
- **Clear error surfacing**: when Scopus returns 401/403, the tool raises `ScopusAccessError` with the actual Elsevier `statusText` plus actionable hints — no more silent empty responses.
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

### What Elsevier officially documents

The [Elsevier Developer Portal](https://dev.elsevier.com/api_docs.html) lists every API but **does not publish a per-endpoint subscription matrix**. The only access notes published are:

- A general statement: *"Some VIEWs are restricted based on subscription status to an Elsevier product."*
- About the institutional token (verbatim from [Elsevier's auth doc](https://dev.elsevier.com/tecdoc_api_authentication.html)):
  > *"An insttoken is an additional security token submitted in tandem with your APIKey. […] The insttoken represents full access to a customer account within our authentication and entitlements system."*
- [Embase](https://www.elsevier.com/products/embase) is officially a **separate product** with its own subscription tiers; having Scopus access does not imply having Embase access.

### What we observed during development

These are the empirical results from testing every tool with one specific institutional API key + insttoken (Université Paris-Dauphine subscription). **Your access will differ** depending on what your institution subscribes to.

| Tool / endpoint | Result with our test key |
|---|---|
| `search_scopus` (basic queries: TITLE-ABS-KEY, AUTH, AU-ID, AFFIL, etc.) | ✅ 200 OK |
| `search_scopus` with `REFEID(...)` (forward-citation field) | ❌ 400 — *"Use of certain field restrictions in the search query is not allowed for this requestor"* |
| `search_authors`, `search_affiliations`, `get_author_profile`, `get_affiliation` | ✅ 200 OK (with insttoken) |
| `search_journals`, `get_journal_by_issn` | ✅ 200 OK |
| `get_abstract_details`, `get_abstract_by_doi`, `get_abstract_references` | ✅ 200 OK |
| `search_sciencedirect` | ✅ 200 OK |
| `get_article` (`view=META_ABS` and `view=FULL`) | ✅ 200 OK — full article body delivered when entitled |
| `get_objects`, `download_object` | ✅ 200/300 OK — figures and supplementary files downloadable |
| `check_article_access` (uses `view=ENTITLED`) | ✅ 200 OK — returns `ENTITLED` / `OPEN_ACCESS` / `NOT_FOUND` |
| `get_plumx_metrics` | ✅ 200 OK |
| `get_quota_status` | ✅ Always works (reads cached headers) |
| `get_article_entitlement` (raw entitlement endpoint) | ❌ 403 — *"Requestor configuration settings insufficient for access to this resource"* |
| `get_citations_overview` | ❌ 403 (same message) |
| `get_citation_count` | ❌ 403 (same message) |
| `get_holdings_report` | ❌ 403 (same message) |
| `get_embase_record` | ❌ 403 (same message — Embase is a separate Elsevier product) |
| `get_citing_papers` (depends on REFEID) | ❌ 400 (same as REFEID above) |

### Practical advice

- **Try a tool to discover access**: any 401/403 raises `ScopusAccessError` with Elsevier's own status text, so you know immediately whether the issue is your key, your insttoken, or your subscription tier.
- **Use `check_article_access(doi)` before bulk full-text downloads** — it's the cheapest way to filter a list of DOIs to those you can actually retrieve.
- **An institutional token (`SCOPUS_INST_TOKEN`) helps a lot**: it unlocked the Author/Affiliation/ScienceDirect endpoints from off-campus in our testing, where they otherwise returned 401. Request one from Elsevier support.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

PRs welcome.

If you want to add a new Elsevier endpoint, follow the existing pattern:
1. Add a client method in `src/scopus_mcp/client.py`
2. Add a parser in `src/scopus_mcp/utils.py` (named `clean_<endpoint>`)
3. Register the tool in `src/scopus_mcp/server.py` (both `list_tools` and `call_tool` handlers)
4. Add a test in `tests/`
