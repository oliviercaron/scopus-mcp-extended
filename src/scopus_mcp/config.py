"""Configuration loader for Scopus MCP.

Configuration values come from three sources, in order of precedence:
    1. Process environment (SCOPUS_API_KEY, SCOPUS_INST_TOKEN, CACHE_TTL_*)
    2. ``config.json`` next to the project root, when present
    3. Hard-coded defaults

The values are exposed two ways:

* ``ScopusSettings`` — a frozen dataclass returned by ``load_settings()``,
  resolved once and immutable thereafter. Prefer this for new code.
* Free-standing ``get_api_key()``, ``get_inst_token()``, ``get_cache_config()``
  functions — kept for callers that imported them directly. They simply
  delegate to ``load_settings()``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from dotenv import load_dotenv


# Eagerly populate os.environ from a .env file if one is present.
load_dotenv()


# ---------------------------------------------------------------------- #
# Constants
# ---------------------------------------------------------------------- #

# config.json sits at the repository root, three levels above this file
# (src/scopus_mcp/config.py).
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
_CONFIG_PATH: Path = _PROJECT_ROOT / "config.json"

# Mapping of dataclass field -> (env var name, config.json key, default seconds)
_TTL_FIELDS = (
    ("search_ttl",   "CACHE_TTL_SEARCH",   "cache_ttl_search",   3_600),
    ("abstract_ttl", "CACHE_TTL_ABSTRACT", "cache_ttl_abstract", 2_592_000),
    ("author_ttl",   "CACHE_TTL_AUTHOR",   "cache_ttl_author",   604_800),
    ("default_ttl",  "CACHE_TTL_DEFAULT",  "cache_ttl_default",  86_400),
)


# ---------------------------------------------------------------------- #
# Dataclass
# ---------------------------------------------------------------------- #

@dataclass(frozen=True)
class ScopusSettings:
    """Resolved configuration for a Scopus MCP session."""

    api_key: str
    inst_token: Optional[str] = None
    search_ttl: int = 3_600
    abstract_ttl: int = 2_592_000
    author_ttl: int = 604_800
    default_ttl: int = 86_400

    def cache_ttls(self) -> Dict[str, int]:
        """Re-shape the four TTL fields into the dict that the cache layer
        consumes (keys: ``search``, ``abstract``, ``author``, ``default``)."""
        return {
            "search":   self.search_ttl,
            "abstract": self.abstract_ttl,
            "author":   self.author_ttl,
            "default":  self.default_ttl,
        }


# ---------------------------------------------------------------------- #
# Internal helpers
# ---------------------------------------------------------------------- #

def _read_config_file() -> Dict[str, Any]:
    """Best-effort load of ``config.json``. Errors collapse to ``{}``."""
    if not _CONFIG_PATH.is_file():
        return {}
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_ttl(env_name: str, json_key: str, default: int, file_cfg: Mapping[str, Any]) -> int:
    """Pick a TTL from env -> file -> default, returning an int."""
    raw = os.environ.get(env_name)
    if raw is None:
        raw = file_cfg.get(json_key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------- #
# Single resolver: builds the immutable settings object
# ---------------------------------------------------------------------- #

def load_settings() -> ScopusSettings:
    """Resolve configuration from env + config.json.

    Raises ``ValueError`` if the API key is missing — without it, every
    Scopus API call would fail at the first request.
    """
    file_cfg = _read_config_file()

    # API key — env trumps file; missing in both is a hard error
    api_key = os.environ.get("SCOPUS_API_KEY") or file_cfg.get("api_key")
    if not api_key:
        raise ValueError(
            "Scopus API Key not found. Set the 'SCOPUS_API_KEY' environment "
            "variable, or add 'api_key' to config.json."
        )

    # Insttoken — strictly optional
    inst_token = os.environ.get("SCOPUS_INST_TOKEN") or file_cfg.get("inst_token")
    inst_token = inst_token.strip() if isinstance(inst_token, str) and inst_token.strip() else None

    # TTL fields — uniform resolver across all four
    ttls = {
        field_name: _resolve_ttl(env_name, json_key, default, file_cfg)
        for field_name, env_name, json_key, default in _TTL_FIELDS
    }

    return ScopusSettings(api_key=str(api_key).strip(), inst_token=inst_token, **ttls)


# ---------------------------------------------------------------------- #
# Backwards-compatible facade — preserves the existing import surface
# ---------------------------------------------------------------------- #

def get_api_key() -> str:
    """Return the resolved Scopus API key (raises if absent)."""
    return load_settings().api_key


def get_inst_token() -> Optional[str]:
    """Return the resolved institutional token, or None."""
    return load_settings().inst_token


def get_cache_config() -> Dict[str, int]:
    """Return cache TTLs in the dict shape the cache layer expects."""
    return load_settings().cache_ttls()


# Kept for backwards compatibility with anything that imported the bare
# loader from the previous implementation.
def load_config_file() -> Dict[str, Any]:
    return _read_config_file()
