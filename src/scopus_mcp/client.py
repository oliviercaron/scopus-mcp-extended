"""Async HTTP client for the Elsevier API surface.

This module owns:
  * a thin retry/cache wrapper around httpx
  * one method per supported endpoint (search, retrieval, downloads…)
  * a ScopusAccessError that surfaces tier/auth issues with Elsevier's own
    statusText, instead of returning empty payloads silently
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional
from urllib.parse import urljoin

import httpx

from .cache import CacheManager
from .config import get_api_key, get_cache_config, get_inst_token


logger = logging.getLogger(__name__)
# Library code shouldn't override the root logger, but the previous
# implementation set up a basicConfig in this module — we keep that
# behaviour for callers who relied on the default logging being live.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)


# Root of every Elsevier API URL we hit.
BASE_URL = "https://api.elsevier.com/"

# Status codes that are documented as success on certain endpoints — Elsevier
# replies with "300 Multiple Choices" to the Object Retrieval endpoint when
# multiple representations of an object exist. Anywhere else, treat 3xx as
# an error.
_SUCCESS_3XX_PREFIXES = ("content/object/",)

# HTTP status codes that warrant a transient-error retry (server-side
# instability). 429 is handled separately because it has its own backoff
# semantics driven by X-RateLimit-Reset.
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})


@dataclass(frozen=True)
class _RetryPolicy:
    """Bundle the knobs that drive the request retry loop."""
    max_attempts: int = 3
    initial_backoff: float = 1.0
    backoff_multiplier: float = 2.0


_DEFAULT_RETRY = _RetryPolicy()


# Mapping of probe label → list of MCP tool names that share its access
# requirements. Used by check_capabilities() to translate raw probe results
# into a "what works / what doesn't" view at the tool granularity.
_PROBE_TO_TOOLS = {
    "scopus_search":      ["search_scopus", "search_journals", "get_journal_by_issn",
                           "get_quota_status", "get_bibtex"],
    "author_search":      ["search_authors", "search_affiliations",
                           "get_author_profile", "get_affiliation",
                           "get_author_coauthors"],
    "abstract_retrieval": ["get_abstract_details", "get_abstract_by_doi",
                           "get_abstract_references"],
    "plumx_metrics":      ["get_plumx_metrics"],
    "citation_overview":  ["get_citations_overview", "get_citation_count",
                           "get_citing_papers"],
    "embase_retrieval":   ["get_embase_record"],
}


def _tools_for_probes(results: Dict[str, Dict[str, Any]], success: bool) -> list:
    """Map probe results back to MCP tool names that share each probe's
    access requirements. ``success=True`` returns tools we expect to work;
    ``success=False`` returns tools we expect to fail."""
    out = []
    for probe_label, info in results.items():
        is_ok = info.get("status") == "ok"
        if is_ok == success:
            out.extend(_PROBE_TO_TOOLS.get(probe_label, []))
    return sorted(set(out))


# Sentinel objects used by the request pipeline to signal what to do after
# an HTTP error. Object identity is the comparison key — `is` checks only.
_RETRY_TRANSIENT = object()
_GIVE_UP_EMPTY = object()


class _RetryRequest(Exception):
    """Internal control-flow exception: tells _request to retry the call.

    Two sub-cases:
      * is_rate_limit=True → sleep `sleep_for` seconds then retry without
        decrementing the attempt counter.
      * is_rate_limit=False → standard exponential backoff. `cause` carries
        the originating exception for the eventual error message.
    """
    def __init__(self, *, is_rate_limit: bool, sleep_for: float = 0.0, cause: Optional[BaseException] = None):
        super().__init__("internal retry signal")
        self.is_rate_limit = is_rate_limit
        self.sleep_for = sleep_for
        self.cause = cause


class ScopusAccessError(Exception):
    """Raised when Scopus refuses access to an endpoint (HTTP 401 or 403).

    Common causes:
      * Missing or invalid API key (see SCOPUS_API_KEY).
      * Missing or invalid institutional token (see SCOPUS_INST_TOKEN /
        X-ELS-Insttoken header).
      * Your institution's Scopus subscription tier does not include this
        specific endpoint. For example, REFEID queries (forward citations)
        and the Citations Overview API are premium add-ons that many
        institutional subscriptions do not bundle.
      * Endpoint requires institutional network access (try via VPN / library
        proxy).
    """
    pass


def _build_request_headers(api_key: str, inst_token: Optional[str]) -> Dict[str, str]:
    """Assemble the constant header dict every Elsevier request reuses."""
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/json",
        "User-Agent": "ScopusMCP/0.1.0",
    }
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    return headers


class ScopusClient:
    """Async wrapper around the Elsevier API surface (Scopus + ScienceDirect
    + PlumX + Embase). One instance owns one ``httpx.AsyncClient`` and one
    ``CacheManager``; share it across calls in the same session.
    """

    def __init__(self, *, retry_policy: _RetryPolicy = _DEFAULT_RETRY):
        self.api_key: str = get_api_key()
        self.cache_config: Dict[str, int] = get_cache_config()
        self._retry_policy = retry_policy

        inst_token = get_inst_token()
        self.headers: Dict[str, str] = _build_request_headers(self.api_key, inst_token)
        if inst_token:
            logger.info(
                "Institutional token configured — IP-restricted endpoints enabled."
            )

        self.cache = CacheManager(expiration_seconds=self.cache_config["default"])
        self.client = httpx.AsyncClient(
            headers=self.headers,
            timeout=30.0,
            follow_redirects=True,
        )
        # Last-seen rate limit headers, updated after every response (success
        # or error). Exposed via get_quota_status().
        self.quota_info: Dict[str, str] = {}

    async def close(self) -> None:
        """Release the underlying httpx pool."""
        await self.client.aclose()

    async def get_quota_status(self) -> Dict[str, Any]:
        """Latest rate-limit snapshot collected by ``_capture_quota``."""
        return self.quota_info

    # ------------------------------------------------------------------ #
    # Request pipeline                                                   #
    #                                                                    #
    # The pipeline is split into small steps:                            #
    #   _request          → the public entrypoint, owns cache + retries  #
    #   _attempt          → one HTTP round-trip, returns the parsed JSON #
    #                       or raises (HTTPStatusError / RequestError)   #
    #   _handle_status    → translates HTTPStatusError into a sentinel:  #
    #                       "retry" / "give-up-empty" / explicit raise   #
    #   _capture_quota    → side-effect: store last-seen quota headers   #
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
        ttl: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Issue a request to the Elsevier API with caching + retry."""
        url = urljoin(BASE_URL, endpoint)
        verb = method.upper()
        cacheable = use_cache and verb == "GET"

        if cacheable:
            cached = self.cache.get(url, params)
            if cached is not None:
                logger.debug("Cache hit for %s", url)
                return cached

        last_error: Optional[BaseException] = None
        attempts_left = self._retry_policy.max_attempts
        backoff = self._retry_policy.initial_backoff

        while attempts_left > 0:
            try:
                payload = await self._attempt(verb, url, endpoint, params)
            except _RetryRequest as wait_signal:
                # Either rate-limit (sleep until reset) or transient 5xx
                # (exponential backoff). Either way: don't decrement
                # attempts when waiting on a rate-limit reset.
                if wait_signal.is_rate_limit:
                    await asyncio.sleep(wait_signal.sleep_for)
                    continue
                await asyncio.sleep(backoff)
                attempts_left -= 1
                backoff *= self._retry_policy.backoff_multiplier
                last_error = wait_signal.cause
                continue
            except httpx.RequestError as exc:
                logger.warning("Request to %s failed: %s — retrying", url, exc)
                await asyncio.sleep(backoff)
                attempts_left -= 1
                backoff *= self._retry_policy.backoff_multiplier
                last_error = exc
                continue

            # Successful payload — cache it (when applicable) and return.
            if cacheable:
                self.cache.set(url, payload, params, ttl=ttl)
            return payload

        if last_error is not None:
            raise RuntimeError(f"Max retries exceeded for {url}") from last_error
        raise RuntimeError(f"Max retries exceeded for {url}")

    async def _attempt(
        self,
        verb: str,
        url: str,
        endpoint: str,
        params: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Single HTTP round-trip. Returns parsed JSON or raises."""
        response = await self.client.request(verb, url, params=params)
        self._capture_quota(response.headers)

        status = response.status_code

        # Rate-limit → instruct the outer loop to sleep until reset epoch.
        if status == 429:
            sleep_for = self._rate_limit_sleep(response.headers)
            logger.warning("Rate limit hit on %s — sleeping %.2fs", url, sleep_for)
            raise _RetryRequest(is_rate_limit=True, sleep_for=sleep_for)

        # HTTP 300 ("Multiple Choices") is the documented success
        # response for the Object Retrieval endpoint only — Elsevier
        # returns the list of available formats with this status code.
        # Anywhere else, a 3xx is a real redirect / error, so we still
        # let raise_for_status flag it.
        if status == 300 and any(endpoint.startswith(p) for p in _SUCCESS_3XX_PREFIXES):
            return self._json_or_raise(response)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as http_err:
            # Update quota info even on error if headers exist
            if http_err.response is not None:
                self._capture_quota(http_err.response.headers)
            disposition = self._handle_status_error(http_err, url)
            if disposition is _GIVE_UP_EMPTY:
                return {}
            if disposition is _RETRY_TRANSIENT:
                raise _RetryRequest(is_rate_limit=False, cause=http_err)
            # Anything else → propagate (handler already raised, but be safe)
            raise

        return self._json_or_raise(response)

    @staticmethod
    def _rate_limit_sleep(headers: httpx.Headers) -> float:
        """Compute how long to wait before retrying a 429."""
        try:
            reset_at = int(headers.get("X-RateLimit-Reset", "0"))
        except (TypeError, ValueError):
            reset_at = 0
        return max(reset_at - time.time(), 1.0)

    @staticmethod
    def _json_or_raise(response: httpx.Response) -> Dict[str, Any]:
        """Parse a JSON body, normalizing parse failures into RuntimeError."""
        try:
            return response.json()
        except ValueError as exc:
            logger.error("Failed to parse JSON from %s", response.url)
            raise RuntimeError("Invalid JSON response from Scopus API") from exc

    def _handle_status_error(self, http_err: httpx.HTTPStatusError, url: str):
        """Triage an HTTPStatusError into one of three dispositions:
        retry / give up returning empty / raise (specific or generic)."""
        status = http_err.response.status_code
        if status in _RETRYABLE_STATUSES:
            logger.warning("Server error %s on %s — will retry", status, url)
            return _RETRY_TRANSIENT
        if status in (401, 403):
            # Surface Scopus's own error explanation if present — it's
            # almost always more useful than just "Forbidden". Typical
            # body: {"service-error": {"status": {"statusCode": "...",
            # "statusText": "..."}}}
            scopus_code = ""
            scopus_text = ""
            try:
                body = http_err.response.json() or {}
                st = (body.get("service-error") or {}).get("status") or {}
                scopus_code = st.get("statusCode", "") or ""
                scopus_text = st.get("statusText", "") or ""
            except Exception:
                pass

            msg_parts = [f"HTTP {status} from Scopus on {url}"]
            if scopus_code or scopus_text:
                msg_parts.append(
                    f"Scopus error: {scopus_code} - {scopus_text}".strip(" -")
                )
            msg_parts.append(
                "Likely causes: (1) missing or invalid API key / "
                "institutional token; (2) your Scopus subscription "
                "tier does not include this endpoint — REFEID queries "
                "and the Citations Overview API are premium add-ons "
                "many institutions do not have; (3) the endpoint "
                "requires institutional network access (try via VPN / "
                "library proxy)."
            )
            full_msg = " | ".join(msg_parts)
            logger.warning(full_msg)
            raise ScopusAccessError(full_msg)
        if status == 404:
            logger.info("Resource not found: %s", url)
            return _GIVE_UP_EMPTY
        # Anything else (400, 405, …) → propagate the original HTTP error.
        raise http_err

    def _capture_quota(self, headers: httpx.Headers) -> None:
        """Store the most recent rate-limit headers on the client."""
        self.quota_info = {
            "limit":     headers.get("X-RateLimit-Limit", "unknown"),
            "remaining": headers.get("X-RateLimit-Remaining", "unknown"),
            "reset":     headers.get("X-RateLimit-Reset", "unknown"),
            "status":    "OK",
        }

    # Backwards-compatible alias — earlier code (and any external caller
    # that imported it) used this name.
    _update_quota_info = _capture_quota

    # ------------------------------------------------------------------ #
    # Endpoint helpers + the public method wrappers built on top.
    #
    # Each wrapper is a thin façade over `_get_endpoint()` — it spells out
    # the URL template, the cache TTL bucket, and the public signature.
    # The dispatcher below removes the boilerplate `params = {...}; return
    # await self._request(...)` pattern that would otherwise be repeated
    # ten times.
    # ------------------------------------------------------------------ #

    async def _get_endpoint(
        self,
        path_template: str,
        ttl_bucket: str = "default",
        path_args: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """Resolve ``path_template`` with ``path_args``, then GET it."""
        path = path_template.format(**(path_args or {}))
        return await self._request(
            "GET", path, params=query, use_cache=use_cache,
            ttl=self.cache_config.get(ttl_bucket, self.cache_config["default"]),
        )

    @staticmethod
    def _strip_id_prefix(value: str, prefix: str) -> str:
        """Drop a ``SCOPUS_ID:``/``AUTHOR_ID:`` etc. prefix if present."""
        return value[len(prefix):] if value.startswith(prefix) else value

    # ----- Scopus search ------------------------------------------------- #

    async def search_scopus(
        self,
        query: str,
        count: int = 25,
        start: int = 0,
        sort: str = "coverDate",
        view: str = "STANDARD",
    ) -> Dict[str, Any]:
        """Search the Scopus index. ``view='COMPLETE'`` adds full author lists."""
        return await self._get_endpoint(
            "content/search/scopus",
            ttl_bucket="search",
            query={"query": query, "count": count, "start": start, "sort": sort, "view": view},
        )

    # ----- Abstract retrieval ------------------------------------------- #

    async def get_abstract(self, scopus_id: str) -> Dict[str, Any]:
        """Fetch a Scopus abstract record by Scopus ID."""
        return await self._get_endpoint(
            "content/abstract/scopus_id/{sid}",
            ttl_bucket="abstract",
            path_args={"sid": self._strip_id_prefix(scopus_id, "SCOPUS_ID:")},
        )

    async def get_abstract_by_doi(self, doi: str) -> Dict[str, Any]:
        """Fetch a Scopus abstract record by DOI."""
        return await self._get_endpoint(
            "content/abstract/doi/{doi}",
            ttl_bucket="abstract",
            path_args={"doi": doi},
        )

    # ----- Author retrieval --------------------------------------------- #

    async def get_author(self, author_id: str) -> Dict[str, Any]:
        """Fetch an author profile by Scopus author ID."""
        return await self._get_endpoint(
            "content/author/author_id/{aid}",
            ttl_bucket="author",
            path_args={"aid": self._strip_id_prefix(author_id, "AUTHOR_ID:")},
        )

    async def get_abstract_references(self, scopus_id: str, max_refs: int = 200) -> Dict[str, Any]:
        """
        Retrieves the reference list of a paper (works cited BY this paper),
        i.e. the bibliography. Backward-citation lookup, complement to forward
        citation queries (REFEID/Citations Overview, which require a separate
        Scopus subscription tier).

        Uses the Abstract Retrieval API with view=REF. Scopus paginates by 40
        references per page; this method loops internally via the `startref`
        parameter up to `max_refs` total or until the full reference list is
        fetched.

        Endpoint: content/abstract/scopus_id/{id}?view=REF&startref=N

        Args:
            scopus_id: Scopus document ID (with or without SCOPUS_ID: prefix).
            max_refs: Maximum total references to fetch (default 200).

        Returns:
            dict with keys:
                total_references: int  -- value reported by Scopus
                returned: int          -- how many we actually fetched
                references_raw: list   -- merged raw reference dicts across pages
        """
        clean_id = scopus_id.replace('SCOPUS_ID:', '')
        endpoint = f'content/abstract/scopus_id/{clean_id}'
        page_size = 40

        merged: list = []
        total_refs = None
        start_ref = 1

        while True:
            params: Dict[str, Any] = {'view': 'REF'}
            if start_ref > 1:
                params['startref'] = start_ref
            data = await self._request(
                'GET', endpoint, params, ttl=self.cache_config['abstract']
            )
            block = ((data or {}).get('abstracts-retrieval-response') or {}).get('references') or {}
            if total_refs is None:
                try:
                    total_refs = int(block.get('@total-references', 0))
                except (TypeError, ValueError):
                    total_refs = 0
            page = block.get('reference') or []
            if isinstance(page, dict):
                page = [page]
            if not page:
                break
            merged.extend(page)
            if len(merged) >= total_refs or len(merged) >= max_refs:
                break
            start_ref += page_size

        # Honour max_refs strictly — Scopus pages by 40 so the last page can
        # push us past the requested cap.
        if len(merged) > max_refs:
            merged = merged[:max_refs]

        return {
            'total_references': total_refs or 0,
            'returned': len(merged),
            'references_raw': merged,
        }

    # ----- Affiliation search + retrieval ------------------------------- #

    async def search_affiliations(self, query: str, count: int = 10) -> Dict[str, Any]:
        """Search Scopus institutions matching ``query``."""
        return await self._get_endpoint(
            "content/search/affiliation",
            ttl_bucket="search",
            query={"query": query, "count": count},
        )

    async def get_affiliation(self, affiliation_id: str) -> Dict[str, Any]:
        """Fetch the full institution profile by Scopus Affiliation ID."""
        return await self._get_endpoint(
            "content/affiliation/affiliation_id/{aid}",
            ttl_bucket="default",
            path_args={"aid": self._strip_id_prefix(affiliation_id, "AFFILIATION_ID:")},
        )

    # ----- Author search ------------------------------------------------ #

    async def search_authors(self, query: str, count: int = 10) -> Dict[str, Any]:
        """Search authors by name or affiliation. Requires Insttoken on most subs."""
        return await self._get_endpoint(
            "content/search/author",
            ttl_bucket="search",
            query={"query": query, "count": count},
        )

    # ----- Citations overview ------------------------------------------- #

    async def get_citations_overview(
        self, scopus_id: str, date: Optional[str] = None
    ) -> Dict[str, Any]:
        """Year-by-year citation history for a document. Premium tier required."""
        query: Dict[str, Any] = {"scopus_id": self._strip_id_prefix(scopus_id, "SCOPUS_ID:")}
        if date:
            query["date"] = date
        return await self._get_endpoint(
            "content/abstract/citations", ttl_bucket="default", query=query
        )

    # ----- Serial title (journal metrics) ------------------------------- #

    async def get_journal_by_issn(self, issn: str) -> Dict[str, Any]:
        """Journal metadata + metrics (SNIP/SJR/CiteScore) by ISSN."""
        clean_issn = "".join(ch for ch in issn if ch.isalnum())
        return await self._get_endpoint(
            "content/serial/title/issn/{issn}",
            ttl_bucket="abstract",
            path_args={"issn": clean_issn},
        )

    async def search_journals(self, title: str, count: int = 10) -> Dict[str, Any]:
        """Search journals by title keyword. Returns ENHANCED view (with metrics)."""
        return await self._get_endpoint(
            "content/serial/title",
            ttl_bucket="search",
            query={"title": title, "count": count, "view": "ENHANCED"},
        )

    # ------------------------------------------------------------------ #
    # ScienceDirect / full-text / altmetrics endpoints
    # ------------------------------------------------------------------ #

    async def get_plumx_metrics(self, value: str, identifier: str = "doi") -> Dict[str, Any]:
        """
        Retrieves PlumX altmetrics for a research artifact (article, dataset,
        preprint, video, etc.). PlumX captures Twitter mentions, news, blog
        posts, downloads, readers (Mendeley), citations, and more — a much
        broader impact picture than Scopus' citation count alone.

        Endpoint: analytics/plumx/{identifier}/{value}
        Docs: https://dev.elsevier.com/documentation/PlumXMetricsAPI.wadl

        Args:
            value: The identifier value (e.g., "10.1016/j.nicl.2018.10.013").
            identifier: Identifier type. Common values: 'doi', 'pmid', 'pii',
                'isbn', 'arxivId', 'ssrnId', 'githubRepoId', 'youtubeVideoId',
                'figshareArticleId', 'sdEid' (ScienceDirect EID).
                Full list at the WADL link above.
        """
        # We do not hard-validate the identifier against the full PlumX list
        # (33+ values that may evolve); we forward whatever the caller passes.
        endpoint = f'analytics/plumx/{identifier}/{value}'
        return await self._request('GET', endpoint, ttl=self.cache_config['default'])

    async def search_sciencedirect(
        self, query: str, count: int = 10, start: int = 0
    ) -> Dict[str, Any]:
        """
        Full-text search of ScienceDirect (Elsevier's article platform).

        Different from `search_scopus`: Scopus searches bibliographic records
        (title, abstract, keywords); ScienceDirect search searches the full
        text of articles, books and reference works on ScienceDirect when
        available. Returns matching articles with metadata.

        Endpoint: content/search/sciencedirect

        Args:
            query: ScienceDirect search query. Supports the same field-prefix
                syntax as Scopus search where applicable (TITLE-ABS-KEY,
                AUTHOR-NAME, etc.) plus full-text matching on the article body.
            count: Number of results to return (max 100 per request).
            start: Offset for pagination.
        """
        params = {
            'query': query,
            'count': max(1, min(count, 100)),
            'start': max(0, start),
        }
        return await self._request(
            'GET', 'content/search/sciencedirect', params,
            ttl=self.cache_config['search'],
        )

    async def get_article(
        self,
        value: str,
        identifier: str = "doi",
        view: str = "META_ABS",
    ) -> Dict[str, Any]:
        """
        Retrieves a full article from the ScienceDirect Article Retrieval API.

        Whereas `get_abstract_*` returns Scopus' bibliographic record, this
        method hits the ScienceDirect platform and can return the article body
        when the caller is entitled. The 'view' parameter controls how much is
        returned.

        Endpoint: content/article/{identifier}/{value}?view={view}
        Docs: https://dev.elsevier.com/guides/ArticleRetrievalViews.htm

        Args:
            value: The identifier value (DOI, EID, PII, scopus_id, pubmed_id).
            identifier: Identifier type. One of:
                'doi', 'eid', 'pii', 'scopus_id', 'pubmed_id'.
            view: One of 'META', 'META_ABS', 'META_ABS_REF', 'FULL', 'REF',
                'ENTITLED'. 'FULL' includes the original article body; 'REF'
                returns just the reference list; 'ENTITLED' returns whether
                you have access.
        """
        clean_value = value.replace('SCOPUS_ID:', '').replace('DOI:', '')
        params = {'view': view}
        endpoint = f'content/article/{identifier}/{clean_value}'
        return await self._request(
            'GET', endpoint, params, ttl=self.cache_config['abstract']
        )

    async def get_objects(
        self, value: str, identifier: str = "pii"
    ) -> Dict[str, Any]:
        """
        Lists the embedded objects (figures, tables, supplementary files) of
        a ScienceDirect article. Each object comes with download URLs in
        various formats (image/jpeg, image/gif, application/pdf, ...).

        Note: this endpoint typically returns HTTP 300 ("Multiple Choices")
        on success — that is the Elsevier convention for "here is the list of
        formats", not an error. Our _request method treats 2xx and 3xx as
        success (raise_for_status only fires on 4xx/5xx).

        Endpoint: content/object/{identifier}/{value}

        Args:
            value: The identifier value.
            identifier: One of 'doi', 'eid', 'pii', 'scopus_id', 'pubmed_id'.
        """
        clean_value = value.replace('SCOPUS_ID:', '').replace('DOI:', '')
        endpoint = f'content/object/{identifier}/{clean_value}'
        return await self._request(
            'GET', endpoint, ttl=self.cache_config['default']
        )

    async def get_article_entitlement(
        self, value: str, identifier: str = "doi"
    ) -> Dict[str, Any]:
        """
        Asks ScienceDirect whether the current API key + insttoken pair is
        entitled to the FULL TEXT of an article. Useful to check before
        attempting `get_article(view='FULL')`.

        Endpoint: content/article/entitlement/{identifier}/{value}

        Note: this endpoint requires a Scopus subscription tier that includes
        Article Retrieval entitlement checks. Many institutional subscriptions
        do NOT include it (returns 403 with statusCode AUTHENTICATION_ERROR).

        Args:
            value: The identifier value.
            identifier: One of 'doi', 'eid', 'pii', 'scopus_id', 'pubmed_id'.
        """
        clean_value = value.replace('SCOPUS_ID:', '').replace('DOI:', '')
        endpoint = f'content/article/entitlement/{identifier}/{clean_value}'
        return await self._request(
            'GET', endpoint, ttl=self.cache_config['default']
        )

    async def check_capabilities(self) -> Dict[str, Any]:
        """
        Probe a small set of representative endpoints to discover what the
        current API key + insttoken can actually do, without requiring the
        caller to learn Elsevier's access matrix by trial-and-error.

        Runs 6 lightweight requests (one per endpoint family) and classifies
        each into: ``ok`` / ``access_controlled`` / ``not_found`` / ``error``.
        Total cost is ~6 quota units across the various Scopus APIs.

        Caches the result on the client instance so repeated calls within
        a session are free.
        """
        # Memoize: capability state shouldn't change mid-session unless the
        # user re-authenticates, which would mean restarting the process.
        if hasattr(self, "_capabilities_cache"):
            return self._capabilities_cache  # type: ignore[attr-defined]

        # Each probe is (label, async_callable_returning_coroutine, hint).
        # We use harmless lookups (well-known DOIs, common search terms)
        # so a "blocked" result almost certainly means access control,
        # not an empty corpus.
        probes = [
            (
                "scopus_search",
                lambda: self.search_scopus("TITLE(diabetes)", count=1),
                "Core Scopus Search API. Should work for any active subscription.",
            ),
            (
                "author_search",
                lambda: self.search_authors("AUTHLAST(Einstein)", count=1),
                "Author Search API. Off-campus typically requires SCOPUS_INST_TOKEN.",
            ),
            (
                "abstract_retrieval",
                lambda: self.get_abstract_by_doi("10.1016/j.jretconser.2019.01.011"),
                "Abstract Retrieval API. Should work for any subscriber.",
            ),
            (
                "plumx_metrics",
                lambda: self.get_plumx_metrics("10.1016/j.nicl.2018.10.013", "doi"),
                "PlumX Metrics. Per Elsevier docs: 'All active Scopus subscriptions include access'.",
            ),
            (
                "citation_overview",
                lambda: self.get_citations_overview("79955059733"),
                "Citation Overview API. Officially listed by Elsevier as access-controlled (request via support).",
            ),
            (
                "embase_retrieval",
                lambda: self.get_embase_record("10.1016/j.jretconser.2019.01.011", "doi"),
                "Embase is a separate Elsevier product; requires its own subscription.",
            ),
        ]

        results: Dict[str, Dict[str, Any]] = {}
        for label, fn, hint in probes:
            entry: Dict[str, Any] = {"hint": hint}
            try:
                payload = await fn()
                # Heuristic: 404 path returns {} from _request; treat as not_found
                if payload == {}:
                    entry["status"] = "not_found"
                    entry["detail"] = "Endpoint returned 404 (resource not in Elsevier corpus)."
                else:
                    entry["status"] = "ok"
            except ScopusAccessError as exc:
                entry["status"] = "access_controlled"
                # Trim the long error message to just the Scopus statusText line
                msg = str(exc).split(" | ")
                entry["detail"] = msg[1] if len(msg) > 1 else msg[0]
            except Exception as exc:  # network, parse, anything else
                entry["status"] = "error"
                entry["detail"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            results[label] = entry

        # Build a friendly summary for the caller
        ok_count = sum(1 for r in results.values() if r["status"] == "ok")
        blocked = [k for k, r in results.items() if r["status"] == "access_controlled"]
        api_key_present = bool(self.api_key)
        inst_token_present = bool(self.headers.get("X-ELS-Insttoken"))

        summary = (
            f"{ok_count}/{len(probes)} probed endpoints accessible. "
            f"{'Insttoken present' if inst_token_present else 'No insttoken — off-campus access may be limited'}. "
            f"{'Premium/Embase blocked' if blocked else 'No access-controlled endpoints in this probe set are blocked'}."
        )

        snapshot = {
            "auth": {
                "api_key_present": api_key_present,
                "inst_token_present": inst_token_present,
            },
            "summary": summary,
            "probes": results,
            "tools_likely_to_work": _tools_for_probes(results, success=True),
            "tools_likely_to_fail": _tools_for_probes(results, success=False),
            "documentation": "https://github.com/oliviercaron/scopus-mcp-extended#endpoint-access-notes",
        }
        self._capabilities_cache = snapshot  # type: ignore[attr-defined]
        return snapshot

    async def check_article_access(
        self, value: str, identifier: str = "doi"
    ) -> Dict[str, Any]:
        """
        Quickly check whether the current API key + insttoken pair grants
        full-text access to a given article. Wraps the Article Retrieval
        API with view=ENTITLED, which is the lightest call you can make to
        Elsevier (no body, no metadata — just an entitlement decision).

        Returns one of three statuses:
          * 'ENTITLED'     — your subscription covers it; full text downloadable
          * 'OPEN_ACCESS'  — free for everyone; full text downloadable
          * 'NOT_ENTITLED' — in Elsevier's corpus but your subscription excludes it
          * 'NOT_FOUND'    — not in Elsevier's corpus at all (e.g., INFORMS,
                             JSTOR, paywalled non-Elsevier publishers)

        Useful as a pre-filter before bulk-downloading: in a systematic
        review you can call this on a list of DOIs in seconds and only
        attempt full-text retrieval on the ones that will succeed.

        Endpoint: content/article/{identifier}/{value}?view=ENTITLED

        Args:
            value: The identifier value (DOI, EID, PII, scopus_id, pubmed_id).
            identifier: Identifier type (default 'doi').
        """
        try:
            raw = await self.get_article(value, identifier=identifier, view="ENTITLED")
        except ScopusAccessError as e:
            # 401/403 from the Entitlement endpoint typically means the
            # paper is NOT entitled (vs the article not existing).
            return {
                'identifier_type': identifier,
                'identifier_value': value,
                'status': 'NOT_ENTITLED',
                'message': str(e).split(' | ')[1] if ' | ' in str(e) else str(e)[:200],
            }
        # Look at the entitlement decision in the response
        ent = raw.get('document-entitlement') or {}
        if not ent:
            # 404 path: client returns {} (see _request 404 branch).
            # The article isn't in Elsevier's corpus.
            return {
                'identifier_type': identifier,
                'identifier_value': value,
                'status': 'NOT_FOUND',
                'message': 'Article is not in Elsevier ScienceDirect (likely a non-Elsevier publisher).',
            }
        return {
            'identifier_type': identifier,
            'identifier_value': value,
            'status': ent.get('status', 'UNKNOWN'),
            'message': ent.get('message', ''),
            'url': ent.get('url', ''),
        }

    async def get_citation_count(
        self,
        dois: Optional[list] = None,
        eids: Optional[list] = None,
        piis: Optional[list] = None,
        pubmed_ids: Optional[list] = None,
    ) -> Dict[str, Any]:
        """
        Lightweight Citation Count API — returns the bare citation count
        for one or more documents in a single call. ~10x faster than calling
        get_abstract_details() per paper just to read the citedby-count.

        Endpoint: content/abstract/citation-count

        Note: requires a Scopus subscription tier that includes the Citation
        Count API. Many institutions do NOT have it (returns 403 with
        statusCode AUTHENTICATION_ERROR).

        At least one of the four identifier lists must be provided. Each list
        is comma-joined into the query string per Elsevier's convention.
        """
        params: Dict[str, Any] = {}
        if dois:
            params['doi'] = ','.join(dois)
        if eids:
            params['eid'] = ','.join(eids)
        if piis:
            params['pii'] = ','.join(piis)
        if pubmed_ids:
            params['pubmed_id'] = ','.join(pubmed_ids)
        if not params:
            raise ValueError(
                "get_citation_count: at least one of dois/eids/piis/pubmed_ids "
                "must be provided."
            )
        return await self._request(
            'GET', 'content/abstract/citation-count', params,
            ttl=self.cache_config['default'],
        )

    async def get_holdings_report(
        self,
        query: Optional[str] = None,
        start: int = 0,
        count: int = 25,
    ) -> Dict[str, Any]:
        """
        Holdings Report API — lists journals/serials your institution is
        entitled to access via its Elsevier subscription. Useful as a
        pre-filter before searching, so you only target content you can
        actually read.

        Endpoint: holdings/report.url

        Note: requires a Scopus subscription tier that includes the Holdings
        report. Many institutions do NOT have it (returns 403 with
        statusCode AUTHENTICATION_ERROR).

        Args:
            query: optional Scopus-style filter (e.g. SUBJAREA() or ISSN(...)).
            start: pagination offset.
            count: page size.
        """
        params: Dict[str, Any] = {'start': start, 'count': count}
        if query:
            params['query'] = query
        return await self._request(
            'GET', 'holdings/report.url', params,
            ttl=self.cache_config['default'],
        )

    async def download_object(
        self,
        url: str,
        save_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Downloads a binary object (figure, table image, supplementary file,
        sometimes the article PDF) from a URL returned by ``get_objects()``.

        Security model:
          * SSRF: the URL is parsed with ``urllib.parse`` and must satisfy
            scheme=https, host=api.elsevier.com, path under /content/object/,
            no embedded credentials, no path-traversal sequences. Redirects
            are NOT followed (an open redirect on Elsevier's side could
            otherwise bypass the host check).
          * Filesystem: ``save_path`` is treated as a *filename only* (any
            directory component is stripped). The file is always written
            under the configured sandbox directory (defaults to
            ``<tmp>/scopus_mcp_objects/``; override with the
            ``SCOPUS_DOWNLOAD_DIR`` env var). Existing files are NOT
            overwritten — the call fails instead of clobbering.
          * Memory: the response is streamed in 64 KB chunks and capped at
            ``SCOPUS_DOWNLOAD_MAX_BYTES`` (default 100 MB).
          * Cache: bypassed (binary content is not cached).

        Args:
            url: Must be an Elsevier object URL.
            save_path: Optional filename (NOT a path). Default: derived from
                the URL basename + httpAccept mime type extension.

        Returns:
            dict with: ``saved_path``, ``bytes``, ``content_type``,
            ``http_status``, ``url``.
        """
        import os
        import tempfile
        import urllib.parse
        from pathlib import Path

        # ---- 1. Canonical SSRF check on the URL --------------------------
        if not isinstance(url, str):
            raise ValueError(
                f"download_object: url must be a string. Got "
                f"{type(url).__name__}"
            )
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme != 'https':
            raise ValueError(
                f"download_object: url must use https. Got scheme="
                f"{parsed.scheme!r}"
            )
        if parsed.hostname != 'api.elsevier.com':
            raise ValueError(
                f"download_object: url host must be api.elsevier.com. "
                f"Got: {parsed.hostname!r}"
            )
        if parsed.username or parsed.password:
            raise ValueError(
                "download_object: URLs with embedded credentials are rejected."
            )
        # Reject path-traversal in BOTH the raw path and its percent-decoded
        # form. `%2E%2E%2F` decodes to `../` and would otherwise sneak past
        # a literal-string check. We also reject any percent-encoded slashes
        # or backslashes in the path — legitimate Elsevier object endpoint
        # URLs never contain encoded path separators, so seeing them is a
        # strong signal of a probing attempt.
        raw_path_lower = parsed.path.lower()
        decoded_path = urllib.parse.unquote(parsed.path)
        if (
            '/..' in parsed.path
            or '/./' in parsed.path
            or parsed.path.startswith('//')
            or '/..' in decoded_path
            or '/./' in decoded_path
            or '\\' in decoded_path
            or '%2f' in raw_path_lower   # encoded /
            or '%5c' in raw_path_lower   # encoded \
            or '%2e' in raw_path_lower   # encoded .
        ):
            raise ValueError(
                "download_object: URL path-traversal sequences are rejected."
            )
        if not parsed.path.startswith('/content/object/'):
            raise ValueError(
                f"download_object: url path must start with /content/object/. "
                f"Got: {parsed.path!r}"
            )

        # ---- 2. Resolve sandbox dir + safe filename ---------------------
        sandbox_root = Path(
            os.environ.get('SCOPUS_DOWNLOAD_DIR')
            or (Path(tempfile.gettempdir()) / 'scopus_mcp_objects')
        ).expanduser().resolve()
        sandbox_root.mkdir(parents=True, exist_ok=True)

        if save_path is None:
            # Derive filename from URL basename + httpAccept mime extension
            url_basename = Path(parsed.path).name or 'object'
            ext = ''
            qs = urllib.parse.parse_qs(parsed.query)
            mime = (qs.get('httpAccept') or [''])[0]
            ext_for_mime = {
                'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/png': '.png',
                'application/pdf': '.pdf', 'application/xml': '.xml',
                'text/html': '.html',
            }
            ext = ext_for_mime.get(mime, '')
            filename = url_basename + (ext if not url_basename.lower().endswith(ext) else '')
        else:
            if not isinstance(save_path, str):
                raise ValueError(
                    f"download_object: save_path must be a string. Got "
                    f"{type(save_path).__name__}"
                )
            # Strip any directory component — only the basename is used.
            # This prevents path traversal regardless of input shape.
            filename = Path(save_path).name
            if not filename or filename in ('.', '..'):
                raise ValueError(
                    f"download_object: save_path filename is invalid: "
                    f"{save_path!r}"
                )

        # Final target path (sandbox / filename), resolved and re-checked
        target = (sandbox_root / filename).resolve()
        try:
            target.relative_to(sandbox_root)
        except ValueError:
            # Should be unreachable thanks to .name stripping above, but
            # belt-and-suspenders.
            raise ValueError(
                f"download_object: refused — resolved path {target} escapes "
                f"sandbox {sandbox_root}"
            )

        # Refuse overwrite; this also blocks symlink-based attacks because
        # an existing symlink at the target counts as "exists".
        if target.exists() or target.is_symlink():
            raise ValueError(
                f"download_object: target file already exists, refusing to "
                f"overwrite: {target}"
            )

        # ---- 3. Stream the download with size cap -----------------------
        max_bytes = int(
            os.environ.get('SCOPUS_DOWNLOAD_MAX_BYTES', 100 * 1024 * 1024)
        )

        # follow_redirects=False: an open redirect on Elsevier's side could
        # otherwise bypass our host check. The Object Retrieval endpoint is
        # not expected to redirect anyway.
        async with self.client.stream(
            'GET', url, follow_redirects=False
        ) as response:
            # Update quota *before* raise_for_status so a 4xx still records
            # the call against our budget, mirroring _request behaviour.
            self._update_quota_info(response.headers)

            # Content-Length preflight — only the int() parse goes in the
            # try; the over-cap check raises OUTSIDE so it isn't swallowed
            # by `except ValueError`.
            content_length_hdr = response.headers.get('content-length')
            content_length_int: Optional[int] = None
            if content_length_hdr:
                try:
                    content_length_int = int(content_length_hdr)
                except (TypeError, ValueError):
                    # Malformed Content-Length — fall through to the
                    # streaming cap, which will still catch overruns.
                    content_length_int = None
            if content_length_int is not None and content_length_int > max_bytes:
                raise ValueError(
                    f"download_object: object size "
                    f"({content_length_int} bytes) exceeds cap "
                    f"({max_bytes}). Set SCOPUS_DOWNLOAD_MAX_BYTES "
                    f"to override."
                )

            response.raise_for_status()

            total = 0
            try:
                with open(target, 'xb') as fout:  # 'x' = exclusive create
                    try:
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                # Cap exceeded mid-stream. Close + unlink
                                # before raising so the caller doesn't get
                                # a half-written file on disk.
                                fout.close()
                                target.unlink(missing_ok=True)
                                raise ValueError(
                                    f"download_object: stream exceeded max "
                                    f"bytes ({max_bytes}). Aborted; partial "
                                    f"file removed."
                                )
                            fout.write(chunk)
                    except Exception:
                        # Any other failure mid-stream (network reset, write
                        # error, etc.) — clean up the partial file so we
                        # don't leave bytes behind that look like a complete
                        # download. Re-raise so the caller sees the real
                        # error.
                        try:
                            fout.close()
                        except Exception:
                            pass
                        target.unlink(missing_ok=True)
                        raise
            except FileExistsError:
                # Race between the .exists() check and 'xb' open
                raise ValueError(
                    f"download_object: target file appeared concurrently, "
                    f"refusing to overwrite: {target}"
                )

            return {
                'url': url,
                'saved_path': str(target),
                'bytes': total,
                'content_type': response.headers.get('content-type', ''),
                'http_status': response.status_code,
            }

    async def get_embase_record(
        self, value: str, identifier: str = "doi"
    ) -> Dict[str, Any]:
        """
        Retrieves a single record from Embase (Elsevier's biomedical and
        pharmacological database, complementary to MEDLINE/PubMed).

        Endpoint: content/embase/article/{identifier}/{value}

        Note: requires an Embase subscription. Many institutions that have
        Scopus do NOT have Embase (returns 403 AUTHENTICATION_ERROR).

        Args:
            value: The identifier value.
            identifier: One of 'doi', 'pii', 'pubmed_id', 'lui', 'embase',
                'medline'. See the Embase Article Retrieval API docs.
        """
        clean_value = value.replace('DOI:', '')
        endpoint = f'content/embase/article/{identifier}/{clean_value}'
        return await self._request(
            'GET', endpoint, ttl=self.cache_config['abstract']
        )
