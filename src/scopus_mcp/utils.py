"""Response parsers for the Scopus, ScienceDirect, PlumX and Embase APIs.

All parsers consume the raw JSON dict returned by the Elsevier endpoints and
project the relevant subset into a flat, agent-friendly shape. They never
raise on a missing field — every dot-walk goes through ``_pick()`` so partial
or unexpected payloads degrade gracefully to defaults.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


# =====================================================================
# Generic helpers used by every parser
# =====================================================================

_MISSING = object()


def _pick(data: Any, *path: str, default: Any = None) -> Any:
    """Walk a dict path safely.

    ``_pick(d, "a", "b", "c")`` is equivalent to
    ``d.get("a", {}).get("b", {}).get("c")`` but it never crashes when an
    intermediate node is ``None``, a string, or a list — it just returns
    the supplied ``default`` (defaulting to ``None``).
    """
    cursor: Any = data
    for key in path:
        if not isinstance(cursor, dict):
            return default
        nxt = cursor.get(key, _MISSING)
        if nxt is _MISSING:
            return default
        cursor = nxt
    return cursor if cursor is not None else default


def _as_list(value: Any) -> List[Any]:
    """Wrap a single dict in a one-element list. Pass lists through. Anything
    else (None, str, etc.) yields an empty list. Scopus collapses
    single-result entries into a bare object, so most ``entry`` accesses
    need this normalization."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _strip_id_prefix(identifier: Any, prefix: str) -> str:
    """Strip a leading "SCOPUS_ID:"/"AUTHOR_ID:"/"AFFILIATION_ID:" tag."""
    if not isinstance(identifier, str):
        return ""
    return identifier[len(prefix):] if identifier.startswith(prefix) else identifier


def _link_for(links: Iterable[Any], ref_type: str) -> Optional[str]:
    """Return the @href of the first link with ``@ref == ref_type``."""
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("@ref") == ref_type:
            href = link.get("@href")
            if isinstance(href, str):
                return href
    return None


def _scalar_metric(node: Any) -> Optional[str]:
    """Pull the ``$`` (text-content) value out of an Elsevier metric node.

    Elsevier wraps metric numbers in either ``{"$": "1.234"}`` or a list of
    such dicts (one per year). We always read the first element and return
    the string as-is — callers parse to float when they need a number.
    """
    if isinstance(node, dict):
        return node.get("$")
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        first = next(iter(node), None)
        return first.get("$") if isinstance(first, dict) else None
    return None


# Backwards-compatible alias. Older callers imported this helper directly.
_extract_metric_value = _scalar_metric


def _extract_affiliation(profile: Dict[str, Any]) -> Optional[str]:
    """Extract the display name of an author's current institution."""
    affil = _pick(profile, "affiliation-current", "affiliation", default={})
    if isinstance(affil, list):
        affil = next(iter(affil), {})
    if not isinstance(affil, dict):
        return None
    return _pick(affil, "ip-doc", "afdispname")


# =====================================================================
# Parsers — Scopus search & retrieval (one parser per endpoint family,
# all built on _pick + _as_list helpers)
# =====================================================================

def clean_search_results(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Project Scopus Search response entries into one dict per article.

    The ``authors`` field is only populated when the search was issued with
    ``view=COMPLETE``; STANDARD view returns just ``dc:creator`` (the first
    author).
    """
    entries = _as_list(_pick(data, "search-results", "entry"))

    def _author_name(node: Dict[str, Any]) -> str:
        return (
            node.get("authname")
            or node.get("ce:indexed-name")
            or f"{node.get('surname', '')} {node.get('initials', '')}".strip()
        )

    cleaned: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        author_block = _as_list(entry.get("author"))
        author_names = [name for node in author_block if (name := _author_name(node))]
        cleaned.append({
            "scopus_id":        _strip_id_prefix(entry.get("dc:identifier", ""), "SCOPUS_ID:"),
            "title":            entry.get("dc:title"),
            "creator":          entry.get("dc:creator"),
            "authors":          author_names,
            "publication_name": entry.get("prism:publicationName"),
            "cover_date":       entry.get("prism:coverDate"),
            "doi":              entry.get("prism:doi"),
            "cited_by_count":   entry.get("citedby-count"),
            "aggregation_type": entry.get("prism:aggregationType"),
            "url":              _link_for(entry.get("link", []), "scopus"),
        })
    return cleaned


def clean_abstract_details(data: Dict[str, Any]) -> Dict[str, Any]:
    """Project the Abstract Retrieval API response into a flat dict.

    Tries both ``abstracts-retrieval-response`` and the older singular
    ``abstract-retrieval-response`` wrapper. Returns ``{}`` when neither
    is present.
    """
    root = (
        _pick(data, "abstracts-retrieval-response")
        or _pick(data, "abstract-retrieval-response")
    )
    if not isinstance(root, dict):
        return {}

    coredata = _pick(root, "coredata", default={}) or {}

    authors = [
        {
            "auth_id":  node.get("@auid"),
            "name":     node.get("ce:indexed-name"),
            "surname":  node.get("ce:surname"),
            "initials": node.get("ce:initials"),
        }
        for node in _as_list(_pick(root, "authors", "author"))
        if isinstance(node, dict)
    ]

    return {
        "scopus_id":        _strip_id_prefix(coredata.get("dc:identifier", ""), "SCOPUS_ID:"),
        "doi":              coredata.get("prism:doi"),
        "title":            coredata.get("dc:title"),
        "description":      coredata.get("dc:description"),
        "publication_name": coredata.get("prism:publicationName"),
        "cover_date":       coredata.get("prism:coverDate"),
        "cited_by_count":   coredata.get("citedby-count"),
        "authors":          authors,
        "url":              _link_for(coredata.get("link", []), "scopus"),
    }


def clean_author_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    """Project the Author Retrieval API response into a flat author profile.

    Scopus wraps the response in a single-element list in modern versions,
    a bare dict in older ones — we accept both shapes.
    """
    root = _pick(data, "author-retrieval-response")
    if isinstance(root, list):
        root = next(iter(root), None)
    if not isinstance(root, dict):
        return {}

    coredata = _pick(root, "coredata", default={}) or {}
    profile  = _pick(root, "author-profile", default={}) or {}
    pname    = _pick(profile, "preferred-name", default={}) or {}

    return {
        "author_id":           _strip_id_prefix(coredata.get("dc:identifier", ""), "AUTHOR_ID:"),
        "orcid":               coredata.get("orcid"),
        "document_count":      coredata.get("document-count"),
        "cited_by_count":      coredata.get("cited-by-count"),
        "citation_count":      coredata.get("citation-count"),
        "name": {
            "surname":    pname.get("surname"),
            "given_name": pname.get("given-name"),
            "initials":   pname.get("initials"),
        },
        "current_affiliation": _extract_affiliation(profile),
        "url":                 _link_for(coredata.get("link", []), "scopus-author"),
    }


def clean_affiliation_search(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Project Affiliation Search entries into one dict per institution."""
    return [
        {
            "affiliation_id": _strip_id_prefix(entry.get("dc:identifier", ""), "AFFILIATION_ID:"),
            "name":           entry.get("affiliation-name"),
            "document_count": entry.get("document-count"),
            "city":           entry.get("city"),
            "country":        entry.get("country"),
            "url":            _link_for(entry.get("link", []), "scopus-affiliation"),
        }
        for entry in _as_list(_pick(data, "search-results", "entry"))
        if isinstance(entry, dict)
    ]


def clean_affiliation_details(data: Dict[str, Any]) -> Dict[str, Any]:
    """Project the Affiliation Retrieval response into one dict."""
    root = _pick(data, "affiliation-retrieval-response")
    if not isinstance(root, dict):
        return {}

    coredata    = _pick(root, "coredata", default={}) or {}
    institution = _pick(root, "institution-profile", default={}) or {}
    address     = _pick(institution, "address", default={}) or {}

    pname = institution.get("preferred-name")
    if isinstance(pname, str):
        name = pname
    elif isinstance(pname, dict):
        name = pname.get("$") or pname.get("#text")
    else:
        name = None

    return {
        "affiliation_id": _strip_id_prefix(coredata.get("dc:identifier", ""), "AFFILIATION_ID:"),
        "name":           name,
        "document_count": coredata.get("document-count"),
        "city":           address.get("city"),
        "country":        address.get("country"),
        "org_type":       institution.get("org-type"),
        "url":            _link_for(coredata.get("link", []), "scopus-affiliation"),
    }


def clean_author_search(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Project Author Search entries into one dict per matched author."""
    cleaned: List[Dict[str, Any]] = []
    for entry in _as_list(_pick(data, "search-results", "entry")):
        if not isinstance(entry, dict):
            continue
        pname = _pick(entry, "preferred-name", default={}) or {}
        affil = entry.get("affiliation-current")
        affil_name = affil.get("affiliation-name") if isinstance(affil, dict) else None
        cleaned.append({
            "author_id":      _strip_id_prefix(entry.get("dc:identifier", ""), "AUTHOR_ID:"),
            "name":           f"{pname.get('surname', '')}, {pname.get('given-name', '')}".strip(", "),
            "document_count": entry.get("document-count"),
            "affiliation":    affil_name,
            "url":            _link_for(entry.get("link", []), "scopus-author"),
        })
    return cleaned


def clean_citations_overview(data: Dict[str, Any]) -> Dict[str, Any]:
    """Project the Citations Overview API into a year→count dict.

    The Elsevier shape is two parallel arrays under deeply-nested wrapper
    nodes: a header row of years and a data row of counts. We zip them.
    """
    root = _pick(data, "abstract-citations-response")
    if not isinstance(root, dict):
        return {}

    year_headers = _as_list(_pick(root, "citeColumnTotalXML", "citeColumnTotal", "cc"))
    matrix_rows  = _as_list(_pick(root, "citeInfoMatrix", "citeInfoMatrixXML", "citationMatrix", "cc"))

    citations_by_year: Dict[str, int] = {}
    try:
        for year_node, count_node in zip(year_headers, matrix_rows):
            year = year_node.get("$", "") if isinstance(year_node, dict) else ""
            count = count_node.get("$", "0") if isinstance(count_node, dict) else "0"
            citations_by_year[year] = int(count) if str(count).isdigit() else 0
    except (AttributeError, TypeError) as exc:
        logger.warning("clean_citations_overview: unexpected structure — %s", exc)

    return {
        "citations_by_year": citations_by_year,
        "total":             sum(citations_by_year.values()),
    }


# ---------- Serial Title (journal metrics) ----------

def _project_journal(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Common projection used by both single-journal and search responses."""
    subjects = [
        node.get("$")
        for node in _as_list(entry.get("subject-area"))
        if isinstance(node, dict) and node.get("$")
    ]
    return {
        "title":         entry.get("dc:title"),
        "issn":          entry.get("prism:issn"),
        "eissn":         entry.get("prism:eIssn"),
        "publisher":     entry.get("dc:publisher"),
        "subject_areas": subjects,
        "snip":          _scalar_metric(_pick(entry, "SNIPList", "SNIP")),
        "sjr":           _scalar_metric(_pick(entry, "SJRList", "SJR")),
        "cite_score":    _pick(entry, "citeScoreYearInfoList", "citeScoreCurrentMetric"),
        "open_access":   entry.get("openaccess"),
    }


# Kept for backwards compatibility with anything that imported this directly.
_clean_single_journal = _project_journal


def clean_journal_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
    """Single-journal lookup by ISSN — returns the first entry, or ``{}``."""
    entries = _as_list(_pick(data, "serial-metadata-response", "entry"))
    if not entries:
        return {}
    first = entries[0]
    return _project_journal(first) if isinstance(first, dict) else {}


def clean_journal_search(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Title-based search — returns one dict per matched journal."""
    return [
        _project_journal(entry)
        for entry in _as_list(_pick(data, "serial-metadata-response", "entry"))
        if isinstance(entry, dict)
    ]


# =====================================================================
# Parsers — references, ScienceDirect, PlumX, Embase, citation-count,
# holdings, entitlement (additional endpoints layered on top)
# =====================================================================

def clean_references(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts the reference list from the get_abstract_references() raw payload
    (Abstract Retrieval API view=REF). Each reference is normalized to a flat
    dict with whatever metadata Scopus exposes (some refs only have a title,
    others have full DOI + year + page range).

    Returns a dict with:
        total_references: int
        returned: int
        references: List[dict]  -- one entry per cited work
    """
    refs_raw = data.get('references_raw', []) or []
    cleaned: List[Dict[str, Any]] = []

    for r in refs_raw:
        # Author list normalization (single dict vs list)
        author_block = (r.get('author-list') or {}).get('author') or []
        if isinstance(author_block, dict):
            author_block = [author_block]
        author_names = [
            a.get('ce:indexed-name') or a.get('ce:surname') or ''
            for a in author_block
            if isinstance(a, dict)
        ]
        author_names = [n for n in author_names if n]

        # Year extracted from prism:coverDate (when present)
        cover_date = r.get('prism:coverDate') or ''
        year = cover_date[:4] if cover_date else ''

        # Volume / issue / pages from volisspag (when present)
        volisspag = r.get('volisspag') if isinstance(r.get('volisspag'), dict) else {}
        voliss = volisspag.get('voliss') if isinstance(volisspag.get('voliss'), dict) else {}
        pagerange = volisspag.get('pagerange') if isinstance(volisspag.get('pagerange'), dict) else {}

        # Title: usually in 'sourcetitle'; fall back to 'title' (rarely populated)
        title = r.get('sourcetitle')
        if not title:
            t = r.get('title')
            if isinstance(t, dict):
                title = t.get('$', '')
            elif isinstance(t, str):
                title = t

        cleaned.append({
            'scopus_id': r.get('scopus-id'),
            'eid': r.get('scopus-eid'),
            'doi': r.get('ce:doi'),
            'pubmed_id': r.get('pubmed-id'),
            'title': title or '',
            'authors': author_names,
            'year': year,
            'cover_date': cover_date,
            'volume': voliss.get('@volume') if voliss else None,
            'issue': voliss.get('@issue') if voliss else None,
            'page_first': pagerange.get('@first') if pagerange else None,
            'page_last': pagerange.get('@last') if pagerange else None,
            'cited_by_count': r.get('citedby-count'),
            'type': r.get('type'),
            'source_url': r.get('url'),
        })

    return {
        'total_references': data.get('total_references', 0),
        'returned': data.get('returned', len(cleaned)),
        'references': cleaned,
    }


def clean_plumx_metrics(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flattens the PlumX altmetrics response into:
        {
          'id_type': str, 'id_value': str,
          'totals': {category_name: total_count, ...},
          'breakdown': {category_name: {count_type: total, ...}, ...}
        }
    """
    if not data:
        return {}
    totals: Dict[str, int] = {}
    breakdown: Dict[str, Dict[str, int]] = {}
    for cat in data.get('count_categories', []) or []:
        cat_name = cat.get('name') or 'unknown'
        try:
            totals[cat_name] = int(cat.get('total', 0))
        except (TypeError, ValueError):
            totals[cat_name] = 0
        breakdown[cat_name] = {}
        for ct in cat.get('count_types', []) or []:
            ct_name = ct.get('name') or 'unknown'
            try:
                breakdown[cat_name][ct_name] = int(ct.get('total', 0))
            except (TypeError, ValueError):
                breakdown[cat_name][ct_name] = 0
    return {
        'id_type': data.get('id_type'),
        'id_value': data.get('id_value'),
        'totals': totals,
        'breakdown': breakdown,
    }


def clean_sciencedirect_search(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts results from the ScienceDirect Search API response.
    Same general shape as Scopus search but with full-text-source metadata.
    """
    if not data or 'search-results' not in data:
        return {'total_results': 0, 'results': []}
    sr = data['search-results']
    entries = sr.get('entry', [])
    if isinstance(entries, dict):
        entries = [entries]
    cleaned = []
    for e in entries:
        # Authors may live under 'authors.author' or 'dc:creator'
        authors = []
        a_block = (e.get('authors') or {}).get('author') if isinstance(e.get('authors'), dict) else None
        if isinstance(a_block, list):
            authors = [a.get('$') or a.get('ce:indexed-name') or '' for a in a_block if isinstance(a, dict)]
        elif isinstance(a_block, dict):
            authors = [a_block.get('$') or a_block.get('ce:indexed-name') or '']
        elif e.get('dc:creator'):
            authors = [e.get('dc:creator')]
        cleaned.append({
            'eid': e.get('eid', ''),
            'pii': e.get('pii', ''),
            'doi': e.get('prism:doi', ''),
            'title': e.get('dc:title', ''),
            'authors': [a for a in authors if a],
            'publication_name': e.get('prism:publicationName', ''),
            'cover_date': e.get('prism:coverDate', ''),
            'open_access': e.get('openaccess', ''),
            'load_date': e.get('load-date', ''),
        })
    return {
        'total_results': sr.get('opensearch:totalResults', '0'),
        'returned': len(cleaned),
        'results': cleaned,
    }


def clean_article_retrieval(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts core metadata from the ScienceDirect Article Retrieval response.
    Returns coredata + a snippet of the original text when 'FULL' view was used.
    """
    root = data.get('full-text-retrieval-response') or {}
    if not root:
        return {}
    core = root.get('coredata', {}) or {}
    # 'originalText' present when view=FULL
    original_text = root.get('originalText')
    text_snippet = ''
    text_length = 0
    if isinstance(original_text, str):
        text_length = len(original_text)
        text_snippet = original_text[:1500]
    return {
        'doi': core.get('prism:doi', ''),
        'eid': core.get('eid', ''),
        'pii': core.get('pii', ''),
        'title': core.get('dc:title', ''),
        'creator': core.get('dc:creator', ''),
        'publication_name': core.get('prism:publicationName', ''),
        'cover_date': core.get('prism:coverDate', ''),
        'volume': core.get('prism:volume', ''),
        'issue': core.get('prism:issueIdentifier', ''),
        'page_range': core.get('prism:pageRange', ''),
        'abstract': core.get('dc:description', ''),
        'open_access': core.get('openaccess', ''),
        'aggregation_type': core.get('prism:aggregationType', ''),
        'has_full_text': bool(original_text),
        'full_text_length': text_length,
        'full_text_snippet': text_snippet,
        'url': core.get('prism:url', ''),
    }


def clean_objects(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flattens the Object Retrieval response (HTTP 300 Multiple Choices) into
    a list of objects with download URLs and inferred mime types.

    Each object: { ref, type, mime_type, url, extension }.
    """
    choices = (data.get('choices') or {}).get('choice') or []
    if isinstance(choices, dict):
        choices = [choices]
    cleaned = []
    for c in choices:
        url = c.get('$', '')
        # Mime type encoded in the query string after `?httpAccept=`
        mime = ''
        if '?httpAccept=' in url:
            mime = url.split('?httpAccept=', 1)[1]
        # Strip the query string for the extension guess
        bare_url = url.split('?', 1)[0]
        # Get the extension from the URL path
        ext = bare_url.rsplit('.', 1)[-1] if '.' in bare_url.rsplit('/', 1)[-1] else ''
        cleaned.append({
            'ref': c.get('@ref', ''),
            'type': c.get('@type', ''),
            'mime_type': mime,
            'url': url,
            'extension': ext,
        })
    return {
        'object_count': len(cleaned),
        'objects': cleaned,
    }


def clean_entitlement(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flattens the Article Entitlement response (when accessible) into a
    simple yes/no + identifier echo.
    Shape: {'entitlement-response': {'document-entitlement': [{'@status': 'ENTITLED', ...}]}}
    """
    root = data.get('entitlement-response') or {}
    docs = root.get('document-entitlement') or []
    if isinstance(docs, dict):
        docs = [docs]
    cleaned = []
    for d in docs:
        cleaned.append({
            'status': d.get('@status') or d.get('status', ''),
            'eid': d.get('eid', '') or d.get('@eid', ''),
            'doi': d.get('prism:doi', '') or d.get('doi', ''),
            'pii': d.get('pii', ''),
            'message': d.get('message', ''),
        })
    return {'documents': cleaned} if cleaned else data


def clean_citation_count(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flatten the Citation Count API response into a list of
    {identifier_type, identifier, citation_count} entries.

    Response shape varies — Scopus may return either:
      {"citation-count-response": {"document": [{"identifier": "...",
        "citation-count": "42", "scopus_id": "..."}, ...]}}
    or for a single hit, the same with "document" as a bare dict.
    """
    root = data.get('citation-count-response') or {}
    docs = root.get('document') or []
    if isinstance(docs, dict):
        docs = [docs]
    cleaned = []
    for d in docs:
        try:
            cc = int(d.get('citation-count', 0))
        except (TypeError, ValueError):
            cc = 0
        cleaned.append({
            'scopus_id': d.get('scopus_id') or d.get('dc:identifier', '').replace('SCOPUS_ID:', ''),
            'doi': d.get('doi') or d.get('prism:doi'),
            'pii': d.get('pii'),
            'eid': d.get('eid'),
            'citation_count': cc,
        })
    return {
        'document_count': len(cleaned),
        'documents': cleaned,
    }


def clean_holdings_report(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flatten the Holdings Report API response into a list of subscribed
    serials. The exact response key varies across Elsevier versions; we
    accept several known wrappers and fall back to passing through the
    raw payload when the shape is unfamiliar.
    """
    root = (
        data.get('holdings-response')
        or data.get('serial-metadata-response')
        or data.get('holdings')
        or {}
    )
    if not root:
        return {'returned': 0, 'entries': [], 'raw': data}
    entries = root.get('entry') or root.get('serial') or []
    if isinstance(entries, dict):
        entries = [entries]
    cleaned = []
    for e in entries:
        cleaned.append({
            'title': e.get('dc:title') or e.get('title'),
            'issn': e.get('prism:issn') or e.get('issn'),
            'eissn': e.get('prism:eIssn') or e.get('eissn'),
            'publisher': e.get('dc:publisher') or e.get('publisher'),
            'access_type': e.get('access-type') or e.get('@status') or e.get('status'),
            'subscribed': e.get('subscribed') or e.get('entitled'),
        })
    return {
        'returned': len(cleaned),
        'entries': cleaned,
    }


def clean_embase_record(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flattens the Embase Article Retrieval response. Embase records have a
    different schema from Scopus abstracts; we surface the most useful
    bibliographic fields and return the rest as `raw`.
    """
    root = (
        data.get('embase-retrieval-response')
        or data.get('embase-article-response')
        or {}
    )
    if not root:
        return {}
    item = root.get('item') or root
    bib = item.get('bibrecord') or {}
    head = bib.get('head') or {}
    citation = head.get('citation-info') or {}
    return {
        'embase_id': item.get('itemidlist', {}).get('itemid', ''),
        'doi': citation.get('doi', '') or item.get('doi', ''),
        'pubmed_id': citation.get('pubmed-id', ''),
        'title': (head.get('citation-title') or {}).get('titletext', ''),
        'publication_year': citation.get('publicationyear', {}).get('@first', ''),
        'raw': item,
    }


# =====================================================================
# Pure-Python utility composers (no new endpoint, format conversions)
# =====================================================================

# Characters with a special meaning in BibTeX/LaTeX that must be escaped
# inside `{...}` field values. Order matters: backslash must come first,
# then braces, then everything else.
_BIBTEX_ESCAPES = (
    ('\\', '\\textbackslash{}'),
    ('{', '\\{'),
    ('}', '\\}'),
    ('&', '\\&'),
    ('%', '\\%'),
    ('$', '\\$'),
    ('#', '\\#'),
    ('_', '\\_'),
    ('^', '\\textasciicircum{}'),
    ('~', '\\textasciitilde{}'),
)


def _bibtex_escape(value: str) -> str:
    """Escape a string for safe use as a BibTeX `{...}` field value."""
    if not value:
        return ''
    out = str(value)
    for ch, esc in _BIBTEX_ESCAPES:
        out = out.replace(ch, esc)
    return out


def _bibtex_cite_key(raw: str) -> str:
    """Reduce an arbitrary string to ASCII letters/digits for a cite key."""
    return ''.join(ch for ch in (raw or '') if ch.isalnum()) or 'untitled'


def to_bibtex(article_meta: Dict[str, Any]) -> str:
    """
    Convert a cleaned article metadata dict (from clean_abstract_details OR
    clean_article_retrieval) into a single BibTeX entry string ready to paste
    into a .bib file or import into Zotero/Mendeley.
    """
    if not article_meta:
        return ""
    doi = article_meta.get('doi', '') or ''
    eid = article_meta.get('eid', '') or ''
    title = article_meta.get('title') or ''
    journal = article_meta.get('publication_name') or article_meta.get('journal') or ''
    cover_date = article_meta.get('cover_date') or article_meta.get('publication_date') or ''
    year = cover_date[:4] if cover_date else ''
    volume = article_meta.get('volume', '') or ''
    issue = article_meta.get('issue', '') or ''
    pages = article_meta.get('page_range') or article_meta.get('pages', '') or ''
    abstract = article_meta.get('abstract') or ''

    # Authors come in different shapes depending on the source parser:
    #   * clean_abstract_details:    list of dicts {name, surname, initials, auth_id}
    #   * clean_search_results:      list of strings ("Aral S.")
    #   * clean_references:          list of strings ("Aral S.")
    # Normalize to a list of "Surname, Initials" strings.
    def _author_to_str(a):
        if isinstance(a, str):
            return a.strip()
        if isinstance(a, dict):
            surname = a.get('surname') or ''
            initials = a.get('initials') or ''
            if surname:
                return f"{surname}, {initials}".strip(', ')
            return (a.get('name') or '').strip()
        return ''

    authors_field = article_meta.get('authors')
    author_strs: list = []
    if isinstance(authors_field, list):
        author_strs = [s for s in (_author_to_str(a) for a in authors_field) if s]
    authors_str = ' and '.join(author_strs)
    if not authors_str:
        authors_str = article_meta.get('creator', '') or ''

    # BibTeX cite key: first-author surname + year, sanitized to ASCII
    # alphanumerics only (no spaces, no special chars).
    first_author_last = ''
    if author_strs:
        first_author_last = author_strs[0].split(',')[0].split(' ')[0]
    elif authors_str:
        first_author_last = authors_str.split(',')[0].split(' ')[0]
    cite_key = _bibtex_cite_key(f"{first_author_last}{year}") if (first_author_last or year) else _bibtex_cite_key(eid)

    fields = []
    if authors_str:
        fields.append(f"  author = {{{_bibtex_escape(authors_str)}}}")
    if title:
        fields.append(f"  title = {{{_bibtex_escape(title)}}}")
    if journal:
        fields.append(f"  journal = {{{_bibtex_escape(journal)}}}")
    if year:
        fields.append(f"  year = {{{_bibtex_escape(year)}}}")
    if volume:
        fields.append(f"  volume = {{{_bibtex_escape(volume)}}}")
    if issue:
        fields.append(f"  number = {{{_bibtex_escape(issue)}}}")
    if pages:
        fields.append(f"  pages = {{{_bibtex_escape(pages)}}}")
    if doi:
        fields.append(f"  doi = {{{_bibtex_escape(doi)}}}")
    if abstract:
        fields.append(f"  abstract = {{{_bibtex_escape(abstract)}}}")

    body = ',\n'.join(fields)
    return f"@article{{{cite_key},\n{body}\n}}"


def _canonical_author_key(name: str) -> str:
    """
    Reduce an author name string to a comparison-friendly key consisting
    of "<lower-surname> <first-initial>". Handles common shapes:

        "Caron O."           → "caron o"
        "Caron, Olivier"     → "caron o"
        "Olivier Caron"      → "caron o"
        "O. Caron"           → "caron o"
        "Caron-Lizotte O."   → "caron-lizotte o"

    Returns an empty string if no usable parts.
    """
    if not name or not isinstance(name, str):
        return ''
    cleaned = name.strip().rstrip('.')
    if not cleaned:
        return ''
    if ',' in cleaned:
        # "Surname, Given names" → take parts in that order
        surname, _, rest = cleaned.partition(',')
        first_token = rest.strip().split(' ')[0] if rest.strip() else ''
    else:
        tokens = [t for t in cleaned.split(' ') if t]
        if len(tokens) == 1:
            surname, first_token = tokens[0], ''
        else:
            # Disambiguate "Surname I." (last token is initial) vs
            # "Given Surname" (first token is given name).
            last = tokens[-1]
            if len(last.rstrip('.')) <= 2 and last.rstrip('.').isalpha():
                surname = ' '.join(tokens[:-1])
                first_token = last
            else:
                surname = tokens[-1]
                first_token = tokens[0]
    surname = surname.strip().lower()
    first_initial = (first_token.strip().rstrip('.') or '')[:1].lower()
    if not surname:
        return ''
    return f"{surname} {first_initial}".strip()


def aggregate_coauthors(
    author_papers: List[Dict[str, Any]], focal_author_name: str = ''
) -> List[Dict[str, Any]]:
    """
    Given a list of papers (each with an 'authors' list of names), aggregate
    co-author counts excluding the focal author. Returns a sorted list of
    {name, paper_count} descending.

    The focal author is matched on canonical "surname + first-initial" form
    so that "Caron O.", "Caron, Olivier" and "Olivier Caron" all collapse to
    the same key.
    """
    counts: Dict[str, int] = {}
    focal_key = _canonical_author_key(focal_author_name)
    for paper in author_papers:
        names = paper.get('authors') or []
        if not isinstance(names, list):
            continue
        for name in names:
            if not name or not isinstance(name, str):
                continue
            if focal_key and _canonical_author_key(name) == focal_key:
                continue
            counts[name] = counts.get(name, 0) + 1
    return sorted(
        [{'name': n, 'paper_count': c} for n, c in counts.items()],
        key=lambda x: (-x['paper_count'], x['name']),
    )
