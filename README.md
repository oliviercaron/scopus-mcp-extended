# Scopus MCP — Extended

A comprehensive **Model Context Protocol (MCP)** server for the Elsevier Scopus, ScienceDirect, PlumX and Embase APIs. Lets an LLM (Claude, ChatGPT, Cursor, …) search the academic literature, retrieve abstracts, citations, references, full article text, figures, altmetrics, and more — all through your own institutional Elsevier subscription.

## Features

- **25 tools** spanning Scopus Search, Author/Affiliation APIs, ScienceDirect Article Retrieval, PlumX altmetrics, Embase, Holdings — and convenience composers (BibTeX export, co-author networks).
- **Institutional token (`X-ELS-Insttoken`) support** out of the box. Required by Elsevier for off-campus access to Author Search, Affiliation APIs, Article Retrieval, etc.
- **Hardened binary downloads** via `download_object`: SSRF-protected URL validation (canonical `urlparse` checks, no redirect follow, percent-encoded traversal rejected); sandboxed file writes with exclusive-create; streaming with size cap (env-tunable, default 100 MB).
- **Clear error surfacing**: when Scopus returns 401/403, the tool raises `ScopusAccessError` with the actual Elsevier `statusText` plus actionable hints — no more silent empty responses.
- **Per-class HTTP cache** with sensible TTLs (search 1 h, abstract 30 d, author 7 d), automatic retry/backoff on 5xx, request quota tracking.
- **Subscription-tier-aware**: a built-in `check_article_access` tool tells you upfront whether you can full-text a given DOI before bulk downloading.

## Tool inventory (25 total)

### Search
| Tool | Description | Tier |
|---|---|---|
| `search_scopus` | Full-text search of the Scopus index. Supports field-prefix syntax (TITLE-ABS-KEY, AUTH, AFFIL, AU-ID, REFEID...). `view='COMPLETE'` opt-in for full author lists. | Standard |
| `search_authors` | Search authors by name or affiliation. | Insttoken required |
| `search_affiliations` | Search institutions. | Insttoken required |
| `search_journals` | Search journals by title with SJR/SNIP/CiteScore metrics. | Standard |
| `search_sciencedirect` | Full-text search of ScienceDirect — indexes the article body, not just bibliographic metadata. | Standard |

### Retrieve metadata
| Tool | Description | Tier |
|---|---|---|
| `get_abstract_details` | Bibliographic record + abstract by Scopus ID. | Standard |
| `get_abstract_by_doi` | Same, by DOI. | Standard |
| `get_abstract_references` | Reference list (works cited BY this paper). Paginates internally up to `max_refs`. | Standard |
| `get_author_profile` | Author profile: h-index, citation counts, current affiliation. | Insttoken required |
| `get_affiliation` | Institution profile by Scopus Affiliation ID. | Insttoken required |
| `get_journal_by_issn` | Journal metrics (SJR, SNIP, CiteScore) by ISSN. | Standard |

### ScienceDirect article access
| Tool | Description | Tier |
|---|---|---|
| `get_article` | Full article from ScienceDirect — `view='META_ABS'` for metadata + abstract, `view='FULL'` for the article body (when entitled). | Standard for META, full text needs entitlement |
| `get_objects` | List embedded objects of an article: figures, tables, supplementary files, with download URLs in multiple mime types. | Standard |
| `download_object` | Download a binary object (figure, table image, supplementary file) to a sandboxed local folder. SSRF-protected. | Same as `get_objects` |
| `check_article_access` | Quickly answer "do I have full-text access to this DOI?" before bulk downloading. Returns ENTITLED / OPEN_ACCESS / NOT_ENTITLED / NOT_FOUND. | Standard |
| `get_article_entitlement` | Lower-level entitlement check, returns the raw Elsevier response. | Standard |

### Altmetrics
| Tool | Description | Tier |
|---|---|---|
| `get_plumx_metrics` | PlumX altmetrics — Twitter mentions, news, blog posts, downloads, Mendeley readers, citations. Much broader impact picture than just citation counts. | Free for all API keys |

### Forward citations (often premium tier)
| Tool | Description | Tier |
|---|---|---|
| `get_citing_papers` | Papers that CITE a given paper (uses REFEID query). | Premium tier (often blocked) |
| `get_citations_overview` | Year-by-year citation history. | Premium tier (often blocked) |
| `get_citation_count` | Bare citation count for one or more documents (lightweight, batch). | Premium tier (often blocked) |

### Holdings & subscription discovery
| Tool | Description | Tier |
|---|---|---|
| `get_holdings_report` | Lists journals your institution is entitled to access. | Premium tier |

### Embase
| Tool | Description | Tier |
|---|---|---|
| `get_embase_record` | Retrieve a record from Embase (Elsevier's biomedical and pharmacological database). | Embase subscription (separate from Scopus) |

### Composition / utility (no new endpoint)
| Tool | Description |
|---|---|
| `get_bibtex` | Generate a BibTeX entry from a DOI (with proper LaTeX escaping). |
| `get_author_coauthors` | List the most frequent co-authors of an author, ranked by shared paper count. |
| `get_quota_status` | Current API quota (limit / remaining / reset epoch). |

## Quickstart

### 1. Get an Elsevier API key (and optionally an Insttoken)

1. Apply at the [Elsevier Developer Portal](https://dev.elsevier.com/) using your institutional email (free public emails are usually rejected).
2. **Strongly recommended**: also request an *Institutional Token* from Elsevier support. Without it, several endpoints are restricted to requests originating from your institution's IP range. With it, you get the same access from anywhere. See the [Insttoken docs](https://dev.elsevier.com/tecdoc_api_authentication.html) — institutional admins can generate one via the dev portal, or you can email Elsevier support directly explaining your use case.

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

## Subscription tier matrix

The Elsevier API portfolio has multiple tiers. Most institutions have the **Standard** tier; the Premium tier and Embase are separate add-ons that many subscriptions don't include. This MCP works on whatever tier you have — endpoints you don't have access to will raise `ScopusAccessError` with a clear message rather than silently returning empty.

| Endpoint group | Standard | Premium | Embase |
|---|:---:|:---:|:---:|
| Search (Scopus, Authors, Affiliations, Journals) | ✅ | ✅ | ✅ |
| Retrieve (Abstract, Author, Affiliation) | ✅ | ✅ | ✅ |
| ScienceDirect (Article Retrieval, Objects, Search) | ✅ | ✅ | ✅ |
| PlumX | ✅ | ✅ | ✅ |
| `view=ENTITLED` checks | ✅ | ✅ | ✅ |
| Holdings Report | ❌ | ✅ | ❌ |
| REFEID forward citations | ❌ | ✅ | ❌ |
| Citations Overview (year-by-year) | ❌ | ✅ | ❌ |
| Citation Count (lightweight batch) | ❌ | ✅ | ❌ |
| Embase Article Retrieval | ❌ | ❌ | ✅ |

## License

MIT — see [LICENSE](LICENSE).

## Contributing

PRs welcome.

If you want to add a new Elsevier endpoint, follow the existing pattern:
1. Add a client method in `src/scopus_mcp/client.py`
2. Add a parser in `src/scopus_mcp/utils.py` (named `clean_<endpoint>`)
3. Register the tool in `src/scopus_mcp/server.py` (both `list_tools` and `call_tool` handlers)
4. Add a test in `tests/`
