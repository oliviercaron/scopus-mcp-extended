"""MCP server entrypoint for the Scopus / ScienceDirect / PlumX / Embase tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .client import ScopusClient
from .utils import (
    aggregate_coauthors,
    clean_abstract_details,
    clean_affiliation_details,
    clean_affiliation_search,
    clean_article_retrieval,
    clean_author_profile,
    clean_author_search,
    clean_citation_count,
    clean_citations_overview,
    clean_embase_record,
    clean_entitlement,
    clean_holdings_report,
    clean_journal_metadata,
    clean_journal_search,
    clean_objects,
    clean_plumx_metrics,
    clean_references,
    clean_sciencedirect_search,
    clean_search_results,
    to_bibtex,
)


# Library code shouldn't override the root logger; only configure when the
# host application hasn't already set up its own handler.
logger = logging.getLogger("scopus-mcp")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)


server = Server("scopus-mcp")
client = ScopusClient()


# Each tool name maps to an async handler. The dispatcher in
# `handle_call_tool` is a one-line dict lookup over this registry — no
# if/elif chain. Handlers are defined further down and registered via
# `_handler(name)` as a decorator.
ToolHandler = Callable[[Dict[str, Any]], Awaitable[List[types.TextContent]]]
_HANDLERS: Dict[str, ToolHandler] = {}


def _handler(tool_name: str) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator that registers an async function as the handler for a tool."""
    def register(fn: ToolHandler) -> ToolHandler:
        _HANDLERS[tool_name] = fn
        return fn
    return register


def _text(payload: Any) -> List[types.TextContent]:
    """Wrap a value in the single-element TextContent list MCP expects."""
    return [types.TextContent(type="text", text=str(payload))]

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_scopus",
            description="Search for documents in Scopus using a query string.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The Scopus search query (e.g., 'TITLE(AI) AND PUBYEAR > 2020')."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 25).",
                        "default": 5,
                        "maximum": 25
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort order (e.g., 'coverDate', 'relevancy').",
                        "default": "coverDate"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="get_abstract_details",
            description="Retrieve full details for a specific document by Scopus ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "The Scopus ID of the document."
                    }
                },
                "required": ["scopus_id"]
            }
        ),
        types.Tool(
            name="get_author_profile",
            description="Retrieve an author's profile by Author ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "author_id": {
                        "type": "string",
                        "description": "The Scopus Author ID."
                    }
                },
                "required": ["author_id"]
            }
        ),
        types.Tool(
            name="get_citing_papers",
            description="Retrieve a list of papers that have cited the specified document (Forward Citations).",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "The Scopus ID of the document to find citations for."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 25).",
                        "default": 5,
                        "maximum": 25
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort order (e.g., 'coverDate', 'relevancy').",
                        "default": "coverDate"
                    }
                },
                "required": ["scopus_id"]
            }
        ),
        types.Tool(
            name="get_quota_status",
            description="Get the current API quota status (remaining/limit). Note: Values are updated only after making a request.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        types.Tool(
            name="get_abstract_by_doi",
            description="Retrieve full details for a specific document using its DOI.",
            inputSchema={
                "type": "object",
                "properties": {
                    "doi": {
                        "type": "string",
                        "description": "The DOI of the document (e.g., '10.1016/j.jretai.2022.01.001')."
                    }
                },
                "required": ["doi"]
            }
        ),
        types.Tool(
            name="search_affiliations",
            description="Search for academic institutions or research affiliations in Scopus.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Institution name or keyword (e.g., 'Dauphine' or 'AFFIL(Paris)')."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 10, max 25).",
                        "default": 10,
                        "maximum": 25
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="get_affiliation",
            description="Retrieve the full profile of an institution by its Scopus Affiliation ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "affiliation_id": {
                        "type": "string",
                        "description": "The Scopus Affiliation ID (e.g., '60014025')."
                    }
                },
                "required": ["affiliation_id"]
            }
        ),
        types.Tool(
            name="search_authors",
            description="Search for researchers/authors by name or affiliation in Scopus.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Author name or Scopus query (e.g., 'AUTHLAST(Kotler) AND AUTHFIRST(Philip)' or 'AFFIL(Dauphine) AND SUBJAREA(BUSI)')."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 10, max 25).",
                        "default": 10,
                        "maximum": 25
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="get_citations_overview",
            description="Retrieve the year-by-year citation history for a document.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "The Scopus ID of the document."
                    },
                    "date": {
                        "type": "string",
                        "description": "Optional year range to filter citations (e.g., '2018-2023')."
                    }
                },
                "required": ["scopus_id"]
            }
        ),
        types.Tool(
            name="get_abstract_references",
            description=(
                "Retrieve the reference list (works cited BY this paper, i.e. its bibliography) "
                "via Abstract Retrieval API view=REF. Backward-citation lookup — returns the "
                "papers in this article's bibliography. Complement to forward-citation queries "
                "(REFEID/Citations Overview) which require a separate Scopus subscription tier. "
                "Paginates internally (Scopus returns 40 refs per page) up to max_refs total."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "Scopus document ID (e.g., '79955059733'). The 'SCOPUS_ID:' prefix is stripped if present."
                    },
                    "max_refs": {
                        "type": "integer",
                        "description": "Maximum number of references to retrieve (default 200). Scopus paginates by 40 per request, so e.g. 200 = up to 5 paged calls.",
                        "default": 200,
                        "minimum": 1
                    }
                },
                "required": ["scopus_id"]
            }
        ),
        types.Tool(
            name="get_journal_by_issn",
            description="Retrieve journal metadata and metrics (SJR, SNIP, CiteScore) by ISSN.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issn": {
                        "type": "string",
                        "description": "The ISSN of the journal (e.g., '0022-4359' or '00224359')."
                    }
                },
                "required": ["issn"]
            }
        ),
        types.Tool(
            name="search_journals",
            description="Search for academic journals by title keyword and retrieve their metrics (SJR, SNIP, CiteScore).",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Journal title or keyword (e.g., 'Journal of Marketing')."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 10, max 25).",
                        "default": 10,
                        "maximum": 25
                    }
                },
                "required": ["title"]
            }
        ),
        # ==============================================================
        # ScienceDirect / PlumX / Embase / utility tools
        # ==============================================================
        types.Tool(
            name="get_plumx_metrics",
            description=(
                "Retrieve PlumX altmetrics for a research artifact (article, "
                "preprint, dataset, video, etc.). Captures Twitter mentions, "
                "news, blog posts, downloads, Mendeley readers, citations — "
                "a much broader impact picture than the Scopus citation count "
                "alone. Returns flattened totals + breakdown by category."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Identifier value, e.g. '10.1016/j.nicl.2018.10.013' for a DOI."
                    },
                    "identifier": {
                        "type": "string",
                        "description": "Identifier type. Common: 'doi', 'pmid', 'pii', 'isbn', 'arxivId', 'ssrnId', 'githubRepoId', 'youtubeVideoId', 'sdEid'.",
                        "default": "doi"
                    }
                },
                "required": ["value"]
            }
        ),
        types.Tool(
            name="search_sciencedirect",
            description=(
                "Full-text search of ScienceDirect (Elsevier's article "
                "platform). Different from search_scopus: this searches the "
                "actual full text of articles, not just the bibliographic "
                "record. Returns matching articles with metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "ScienceDirect search query. Supports field-prefix syntax (TITLE-ABS-KEY, AUTHOR-NAME, ...)."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 10, max 100).",
                        "default": 10,
                        "maximum": 100
                    },
                    "start": {
                        "type": "integer",
                        "description": "Offset for pagination.",
                        "default": 0
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="get_article",
            description=(
                "Retrieve a full article from the ScienceDirect Article "
                "Retrieval API. Different from get_abstract_*: this hits "
                "ScienceDirect (not Scopus) and can return the article body "
                "when entitled. The 'view' parameter controls how much is "
                "returned (META = bibliographic only, FULL = with originalText)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Identifier value (DOI, EID, PII, scopus_id, pubmed_id)."
                    },
                    "identifier": {
                        "type": "string",
                        "description": "Identifier type.",
                        "enum": ["doi", "eid", "pii", "scopus_id", "pubmed_id"],
                        "default": "doi"
                    },
                    "view": {
                        "type": "string",
                        "description": "How much to return. FULL includes the article body; META is bibliographic only.",
                        "enum": ["META", "META_ABS", "META_ABS_REF", "FULL", "REF", "ENTITLED"],
                        "default": "META_ABS"
                    }
                },
                "required": ["value"]
            }
        ),
        types.Tool(
            name="get_objects",
            description=(
                "List the embedded objects (figures, tables, supplementary "
                "files) of a ScienceDirect article, with download URLs in "
                "various formats (image/jpeg, image/gif, application/pdf, …). "
                "Note: the Elsevier API returns HTTP 300 'Multiple Choices' "
                "as the success response for this endpoint — that is normal."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Identifier value."
                    },
                    "identifier": {
                        "type": "string",
                        "description": "Identifier type.",
                        "enum": ["doi", "eid", "pii", "scopus_id", "pubmed_id"],
                        "default": "pii"
                    }
                },
                "required": ["value"]
            }
        ),
        types.Tool(
            name="get_article_entitlement",
            description=(
                "Ask ScienceDirect whether the current API key + insttoken "
                "pair is entitled to the FULL TEXT of an article. Useful "
                "before attempting get_article(view='FULL'). NOTE: this "
                "endpoint requires a Scopus subscription tier that includes "
                "Article Retrieval entitlement checks; many institutional "
                "subscriptions do NOT include it (returns 403)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Identifier value."
                    },
                    "identifier": {
                        "type": "string",
                        "description": "Identifier type.",
                        "enum": ["doi", "eid", "pii", "scopus_id", "pubmed_id"],
                        "default": "doi"
                    }
                },
                "required": ["value"]
            }
        ),
        types.Tool(
            name="get_embase_record",
            description=(
                "Retrieve a single record from Embase (Elsevier's biomedical "
                "and pharmacological database, complementary to MEDLINE). "
                "NOTE: requires an Embase subscription. Many institutions "
                "that have Scopus do NOT have Embase (returns 403)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Identifier value."
                    },
                    "identifier": {
                        "type": "string",
                        "description": "Identifier type.",
                        "enum": ["doi", "pii", "pubmed_id", "lui", "embase", "medline"],
                        "default": "doi"
                    }
                },
                "required": ["value"]
            }
        ),
        types.Tool(
            name="get_bibtex",
            description=(
                "Generate a BibTeX entry for an article, ready to paste into "
                "a .bib file or import into Zotero/Mendeley. Internally "
                "fetches the article via Scopus Abstract Retrieval (by DOI) "
                "and formats the result. Composition tool — no new endpoint."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "doi": {
                        "type": "string",
                        "description": "DOI of the article (e.g., '10.1016/j.jretconser.2019.01.011')."
                    }
                },
                "required": ["doi"]
            }
        ),
        types.Tool(
            name="get_author_coauthors",
            description=(
                "List the most frequent co-authors of a given author, "
                "ranked by number of shared papers. Internally calls "
                "search_scopus with AU-ID(...) to fetch the author's recent "
                "papers, then aggregates the author lists. Composition tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "author_id": {
                        "type": "string",
                        "description": "Scopus author ID (e.g., '57214575061')."
                    },
                    "max_papers": {
                        "type": "integer",
                        "description": "Number of recent papers to scan (default 25, max 25 per single Scopus call).",
                        "default": 25,
                        "minimum": 1,
                        "maximum": 25
                    }
                },
                "required": ["author_id"]
            }
        ),
        types.Tool(
            name="check_article_access",
            description=(
                "Quickly check whether your API key + insttoken grants "
                "full-text access to an article. Returns ENTITLED, "
                "OPEN_ACCESS, NOT_ENTITLED, or NOT_FOUND. Wraps the Article "
                "Retrieval API with view=ENTITLED — the lightest call you "
                "can make. Use as a pre-filter before bulk full-text "
                "downloads to only attempt papers that will succeed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Identifier value (DOI, EID, PII, scopus_id, pubmed_id)."
                    },
                    "identifier": {
                        "type": "string",
                        "description": "Identifier type.",
                        "enum": ["doi", "eid", "pii", "scopus_id", "pubmed_id"],
                        "default": "doi"
                    }
                },
                "required": ["value"]
            }
        ),
        types.Tool(
            name="get_citation_count",
            description=(
                "Lightweight Citation Count API — returns the bare citation "
                "count for one or more documents in a single call. Much "
                "faster than calling get_abstract_details() per paper. "
                "Provide at least one of dois/eids/piis/pubmed_ids (each "
                "is a list)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dois": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of DOIs."
                    },
                    "eids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of Scopus EIDs."
                    },
                    "piis": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of ScienceDirect PIIs."
                    },
                    "pubmed_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of PubMed IDs."
                    }
                }
            }
        ),
        types.Tool(
            name="get_holdings_report",
            description=(
                "Holdings Report API — list the journals/serials your "
                "institution is entitled to access via its Elsevier "
                "subscription. Useful as a pre-filter before searching, so "
                "you only target content you can actually read. NOTE: many "
                "subscriptions do NOT include this endpoint (returns 403)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional Scopus-style filter (e.g. SUBJAREA() or ISSN(...))."
                    },
                    "start": {
                        "type": "integer",
                        "description": "Pagination offset.",
                        "default": 0
                    },
                    "count": {
                        "type": "integer",
                        "description": "Page size (default 25).",
                        "default": 25,
                        "maximum": 200
                    }
                }
            }
        ),
        types.Tool(
            name="download_object",
            description=(
                "Download a binary object (figure, table image, "
                "supplementary file, sometimes the article PDF) from a URL "
                "returned by get_objects(). The URL must be on Elsevier's "
                "object endpoint (api.elsevier.com/content/object/) — "
                "arbitrary URLs are rejected. By default the file is saved "
                "to a sandbox folder under the OS temp dir; pass save_path "
                "to override. Returns the saved path + size + content_type."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Object URL from get_objects(), starting with 'https://api.elsevier.com/content/object/'."
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Optional FILENAME to save under (any directory component is stripped for safety). Files are always written under the sandbox directory (default: <tmp>/scopus_mcp_objects/, override via SCOPUS_DOWNLOAD_DIR env var). Default: derived from URL basename + httpAccept mime extension."
                    }
                },
                "required": ["url"]
            }
        ),
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Dispatch by tool name into the registry. Errors become text payloads."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _text(f"Error: Unknown tool: {name}")
    try:
        return await handler(arguments or {})
    except Exception as exc:
        logger.error("Error executing tool %s: %s", name, exc)
        return _text(f"Error: {exc}")


# ----------------- Tool handlers (one async function per tool) ---------- #
# Each is decorated with @_handler("…"); the decorator inserts it into
# _HANDLERS so the dispatcher above can find it. Bodies stay close to
# their tool definitions, organized by API family.


def _require(name: str, args: Dict[str, Any]) -> Any:
    """Pull a required argument or raise a uniform ValueError."""
    value = args.get(name)
    if value in (None, ""):
        raise ValueError(f"{name} is required")
    return value


def _normalize_id_list(arg: Any, label: str) -> Any:
    """Validate that ``arg`` is None or a list of strings.

    Used by ``get_citation_count`` to reject bare-string inputs that would
    otherwise be ``','.join()``ed character-by-character.
    """
    if arg is None:
        return None
    if isinstance(arg, list):
        if any(not isinstance(item, str) for item in arg):
            raise ValueError(
                f"get_citation_count: {label} must be a list of strings; "
                f"got non-string item(s)."
            )
        return arg
    raise ValueError(
        f"get_citation_count: {label} must be a list of strings "
        f"(or omitted), not {type(arg).__name__}."
    )


# ---- Scopus search & retrieval (originally upstream) ----

@_handler("search_scopus")
async def _h_search_scopus(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.search_scopus(
        _require("query", args),
        count=args.get("count", 5),
        sort=args.get("sort", "coverDate"),
    )
    return _text(clean_search_results(raw))


@_handler("get_abstract_details")
async def _h_get_abstract_details(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_abstract(_require("scopus_id", args))
    return _text(clean_abstract_details(raw))


@_handler("get_abstract_by_doi")
async def _h_get_abstract_by_doi(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_abstract_by_doi(_require("doi", args))
    return _text(clean_abstract_details(raw))


@_handler("get_author_profile")
async def _h_get_author_profile(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_author(_require("author_id", args))
    return _text(clean_author_profile(raw))


@_handler("get_citing_papers")
async def _h_get_citing_papers(args: Dict[str, Any]) -> List[types.TextContent]:
    scopus_id = _require("scopus_id", args)
    clean_id = scopus_id.replace("SCOPUS_ID:", "")
    raw = await client.search_scopus(
        f"REFEID({clean_id})",
        count=args.get("count", 5),
        sort=args.get("sort", "coverDate"),
    )
    return _text(clean_search_results(raw))


@_handler("get_quota_status")
async def _h_get_quota_status(args: Dict[str, Any]) -> List[types.TextContent]:
    quota = await client.get_quota_status()
    if not quota:
        return _text(
            "No quota information available yet. Please make a request to initialize."
        )
    return _text(quota)


@_handler("search_affiliations")
async def _h_search_affiliations(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.search_affiliations(_require("query", args), count=args.get("count", 10))
    return _text(clean_affiliation_search(raw))


@_handler("get_affiliation")
async def _h_get_affiliation(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_affiliation(_require("affiliation_id", args))
    return _text(clean_affiliation_details(raw))


@_handler("search_authors")
async def _h_search_authors(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.search_authors(_require("query", args), count=args.get("count", 10))
    return _text(clean_author_search(raw))


@_handler("get_citations_overview")
async def _h_get_citations_overview(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_citations_overview(_require("scopus_id", args), date=args.get("date"))
    return _text(clean_citations_overview(raw))


@_handler("get_journal_by_issn")
async def _h_get_journal_by_issn(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_journal_by_issn(_require("issn", args))
    return _text(clean_journal_metadata(raw))


@_handler("search_journals")
async def _h_search_journals(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.search_journals(_require("title", args), count=args.get("count", 10))
    return _text(clean_journal_search(raw))


# ---- get_abstract_references (added in this fork) ----

@_handler("get_abstract_references")
async def _h_get_abstract_references(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_abstract_references(
        _require("scopus_id", args), max_refs=args.get("max_refs", 200)
    )
    return _text(clean_references(raw))


# ---- ScienceDirect / PlumX / Embase / utility tools ----

@_handler("get_plumx_metrics")
async def _h_get_plumx_metrics(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_plumx_metrics(
        _require("value", args), identifier=args.get("identifier", "doi")
    )
    return _text(clean_plumx_metrics(raw))


@_handler("search_sciencedirect")
async def _h_search_sciencedirect(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.search_sciencedirect(
        _require("query", args),
        count=args.get("count", 10),
        start=args.get("start", 0),
    )
    return _text(clean_sciencedirect_search(raw))


@_handler("get_article")
async def _h_get_article(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_article(
        _require("value", args),
        identifier=args.get("identifier", "doi"),
        view=args.get("view", "META_ABS"),
    )
    return _text(clean_article_retrieval(raw))


@_handler("get_objects")
async def _h_get_objects(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_objects(
        _require("value", args), identifier=args.get("identifier", "pii")
    )
    return _text(clean_objects(raw))


@_handler("get_article_entitlement")
async def _h_get_article_entitlement(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_article_entitlement(
        _require("value", args), identifier=args.get("identifier", "doi")
    )
    return _text(clean_entitlement(raw))


@_handler("get_embase_record")
async def _h_get_embase_record(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_embase_record(
        _require("value", args), identifier=args.get("identifier", "doi")
    )
    return _text(clean_embase_record(raw))


@_handler("check_article_access")
async def _h_check_article_access(args: Dict[str, Any]) -> List[types.TextContent]:
    result = await client.check_article_access(
        _require("value", args), identifier=args.get("identifier", "doi")
    )
    return _text(result)


@_handler("get_citation_count")
async def _h_get_citation_count(args: Dict[str, Any]) -> List[types.TextContent]:
    raw = await client.get_citation_count(
        dois=_normalize_id_list(args.get("dois"), "dois"),
        eids=_normalize_id_list(args.get("eids"), "eids"),
        piis=_normalize_id_list(args.get("piis"), "piis"),
        pubmed_ids=_normalize_id_list(args.get("pubmed_ids"), "pubmed_ids"),
    )
    return _text(clean_citation_count(raw))


@_handler("get_holdings_report")
async def _h_get_holdings_report(args: Dict[str, Any]) -> List[types.TextContent]:
    try:
        start = int(args.get("start", 0))
        count = int(args.get("count", 25))
    except (TypeError, ValueError):
        raise ValueError("get_holdings_report: start and count must be integers.")
    if start < 0:
        raise ValueError(f"get_holdings_report: start must be >= 0, got {start}.")
    if not 1 <= count <= 200:
        raise ValueError(f"get_holdings_report: count must be in 1..200, got {count}.")
    raw = await client.get_holdings_report(
        query=args.get("query"), start=start, count=count
    )
    return _text(clean_holdings_report(raw))


@_handler("download_object")
async def _h_download_object(args: Dict[str, Any]) -> List[types.TextContent]:
    result = await client.download_object(
        _require("url", args), save_path=args.get("save_path")
    )
    return _text(result)


@_handler("get_bibtex")
async def _h_get_bibtex(args: Dict[str, Any]) -> List[types.TextContent]:
    # Composition tool: fetch metadata via Scopus Abstract Retrieval (by DOI)
    # then format as BibTeX.
    raw = await client.get_abstract_by_doi(_require("doi", args))
    return _text(to_bibtex(clean_abstract_details(raw)))


@_handler("get_author_coauthors")
async def _h_get_author_coauthors(args: Dict[str, Any]) -> List[types.TextContent]:
    author_id = _require("author_id", args)
    max_papers = args.get("max_papers", 25)
    # Compose: AU-ID(...) search returns the author's papers; we aggregate
    # the author lists across them. view=COMPLETE is required to get the
    # full author list (default STANDARD only returns the first author).
    raw = await client.search_scopus(
        f"AU-ID({author_id})", count=max_papers, view="COMPLETE"
    )
    papers = clean_search_results(raw)
    if not isinstance(papers, list):
        papers = []
    # Also pull the focal author's name from their profile so we can
    # exclude them from the co-author tally. clean_author_profile returns
    # name as a nested dict {surname, given_name, initials}; search
    # results format author names as "Surname I." — build the same shape
    # from the profile so the comparison matches.
    focal_name = ""
    try:
        profile_raw = await client.get_author(author_id)
        profile = clean_author_profile(profile_raw)
        name_dict = profile.get("name") or {}
        if isinstance(name_dict, dict):
            surname = name_dict.get("surname") or ""
            initials = name_dict.get("initials") or ""
            focal_name = f"{surname} {initials}".strip()
        elif isinstance(name_dict, str):
            focal_name = name_dict
    except Exception as profile_err:
        # Without the focal name we cannot exclude the author from their
        # own coauthor tally. Surface the failure so callers can interpret
        # the resulting list (which will include the focal author
        # themselves under their indexed name).
        logger.warning(
            "get_author_coauthors: profile lookup failed for "
            "author_id=%s (%s); focal-author exclusion is disabled "
            "for this call.",
            author_id, profile_err,
        )
    coauthors = aggregate_coauthors(papers, focal_author_name=focal_name)
    return _text({
        "focal_author_id":   author_id,
        "focal_author_name": focal_name,
        "papers_scanned":    len(papers),
        "coauthors":         coauthors,
    })

@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="research-summary",
            description="Search for papers on a topic and generate a research summary",
            arguments=[
                types.PromptArgument(
                    name="topic",
                    description="The research topic (e.g., 'machine learning healthcare')",
                    required=True
                )
            ]
        ),
        types.Prompt(
            name="author-analysis",
            description="Analyze an author's research impact and recent work",
            arguments=[
                types.PromptArgument(
                    name="author_id",
                    description="The Scopus Author ID",
                    required=True
                )
            ]
        )
    ]

@server.get_prompt()
async def handle_get_prompt(
    name: str, arguments: dict[str, str] | None
) -> types.GetPromptResult:
    if not arguments:
        arguments = {}

    if name == "research-summary":
        topic = arguments.get("topic", "unknown topic")
        return types.GetPromptResult(
            description=f"Research summary for {topic}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=f"Please search specifically for high-cited papers related to '{topic}' published in the last 5 years using the search_scopus tool. Sort by cited references if possible. After retrieving the results, please summarize the key trends and findings in this field."
                    )
                )
            ]
        )

    if name == "author-analysis":
        author_id = arguments.get("author_id", "")
        return types.GetPromptResult(
            description=f"Analysis of author {author_id}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=f"Please call the get_author_profile tool for Author ID '{author_id}'. Based on the returned data, analyze their research impact (citations, h-index if available), identify their main affiliation, and summarize their academic standing."
                    )
                )
            ]
        )

    raise ValueError(f"Unknown prompt: {name}")

async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    finally:
        # Ensure client is closed on shutdown
        await client.close()

def start():
    """Entry point for the package script."""
    asyncio.run(main())

if __name__ == "__main__":
    start()
