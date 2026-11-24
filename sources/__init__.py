"""Data-source plugin registry for LogosUpdater.

On import this package auto-discovers every concrete :class:`DataSource`
subclass defined in any ``sources/*.py`` module (except ``base`` and this
``__init__``) and indexes it by its ``id``.

Public API
----------
* :func:`available` -> ``{id: class}`` of all discovered sources.
* :func:`get_active(cfg)` -> an instantiated :class:`DataSource` for the
  id named in ``cfg['data_source']``, built with
  ``cfg['sources'][id]`` settings.

A plugin module that fails to import (syntax error, missing optional
dependency, ...) is skipped with an English warning rather than crashing
discovery — so one broken third-party file can't take the app down.

Stdlib-only: discovery uses ``pkgutil`` + ``importlib``.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import sys

from .base import API_VERSION, DataSource, Setting, SourceError, Station  # re-exported for convenience

__all__ = ["DataSource", "Station", "Setting", "SourceError", "API_VERSION", "available", "get_active",
           "discover", "import_errors"]

logger = logging.getLogger(__name__)

# A user's own plugins can live in a `sources/` folder in the data directory
# (config.app_dir(): next to the executable in a PyInstaller bundle, LOGOSUPDATER_DATA
# in Docker). From a plain source checkout that folder is this package itself.
def _user_plugin_dir():
    import config
    return os.path.join(config.app_dir(), "sources")


_user_dir = _user_plugin_dir()
if _user_dir and os.path.isdir(_user_dir) and \
        os.path.normcase(os.path.abspath(_user_dir)) not in {os.path.normcase(os.path.abspath(x)) for x in __path__}:  # noqa: F821
    __path__.append(_user_dir)  # noqa: F821 - package __path__


# Modules in this package that are never plugins themselves.
_SKIP_MODULES = {"base", "__init__"}

# Populated lazily by discover(): {source_id: DataSource subclass}.
_REGISTRY: dict[str, type[DataSource]] = {}
_IMPORT_ERRORS: dict[str, str] = {}      # plugin module -> error text (shown in ⚙ Settings)
_DISCOVERED = False
_HTTP = {}                               # cache_dir -> shared fetcher.Fetcher


def _iter_subclasses(cls: type) -> set[type]:
    """Return ``cls`` and all of its subclasses, recursively."""
    found: set[type] = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _iter_subclasses(sub)
    return found


def discover(force: bool = False) -> dict[str, type[DataSource]]:
    """Import every plugin module and collect DataSource subclasses.

    Runs once and caches the result; pass ``force=True`` to re-scan
    (useful in tests). A module that raises on import is logged and
    skipped. Two plugins claiming the same ``id`` -> last one wins, with
    a warning.
    """
    global _DISCOVERED
    if _DISCOVERED and not force:
        return _REGISTRY
    if force:
        _REGISTRY.clear()
        _IMPORT_ERRORS.clear()

    for mod_info in pkgutil.iter_modules(__path__):
        name = mod_info.name
        if name in _SKIP_MODULES or name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:  # noqa: BLE001 - never crash discovery
            logger.warning("Skipping data-source plugin '%s': import failed: %s", name, exc)
            _IMPORT_ERRORS[name] = f"{exc.__class__.__name__}: {exc}"

    for cls in _iter_subclasses(DataSource):
        if not cls.__module__.startswith(__name__ + "."):
            continue        # helpers defined elsewhere (tests, scripts) are not plugins
        sid = getattr(cls, "id", "") or ""
        if not sid:
            # Abstract intermediates / forgotten id -> not a usable plugin.
            continue
        if sid in _REGISTRY and _REGISTRY[sid] is not cls:
            logger.warning(
                "Duplicate data-source id '%s' (%s overrides %s)",
                sid, cls.__name__, _REGISTRY[sid].__name__,
            )
        _REGISTRY[sid] = cls

    _DISCOVERED = True
    return _REGISTRY


def available(listed_only: bool = False) -> dict[str, type[DataSource]]:
    """Return ``{id: DataSource subclass}`` for all discovered plugins.

    ``listed_only`` drops plugins with ``listed = False`` (the template), for the UI.
    """
    return {k: v for k, v in discover().items() if v.listed or not listed_only}


def import_errors() -> dict[str, str]:
    """Plugin modules that failed to import: ``{module: "Error: text"}``."""
    discover()
    return dict(_IMPORT_ERRORS)


def shared_http(cfg: dict):
    """One fetcher.Fetcher per data directory: shared rate limits, budget and cache."""
    from fetcher import Fetcher
    img = (cfg.get("paths") or {}).get("image") or "work/station_logos.img"
    cache = os.path.join(os.path.dirname(os.path.abspath(img)), "http_cache")
    if cache not in _HTTP:
        _HTTP[cache] = Fetcher(cfg.get("fetch") or {}, cache_dir=cache)
    return _HTTP[cache]


def bind(src: DataSource, cfg: dict, progress=None, cancelled=None) -> DataSource:
    """Attach the shared HTTP client (limited by the plugin's fetch_policy) and hooks."""
    if getattr(src, "api_version", 1) < API_VERSION:
        logger.warning("Data-source plugin '%s' uses plugin API v%s; please update it to v%d "
                       "(see docs/SOURCES.md). Default fetch limits apply.",
                       src.id, getattr(src, "api_version", 1), API_VERSION)
    policy = src.fetch_policy if getattr(src, "api_version", 1) >= API_VERSION else type(src.fetch_policy)()
    http = shared_http(cfg).for_policy(policy) if policy.network else None
    return src.bind(http, progress, cancelled)


def get_active(cfg: dict) -> DataSource:
    """Instantiate the data source selected by ``cfg['data_source']``.

    ``cfg['data_source']`` is the plugin id; the instance is built with
    ``cfg.get('sources', {}).get(id, {})`` as its settings. Raises
    :class:`KeyError`/:class:`ValueError` with a clear English message
    (listing the available ids) when the id is missing or unknown.
    """
    sources = discover()
    source_id = cfg.get("data_source")
    if not source_id:
        raise KeyError(
            "config is missing 'data_source'. Available sources: "
            + (", ".join(sorted(sources)) or "<none>")
        )
    cls = sources.get(source_id)
    if cls is None:
        raise ValueError(
            f"Unknown data_source '{source_id}'. Available sources: "
            + (", ".join(sorted(sources)) or "<none>")
        )
    settings = cfg.get("sources", {}).get(source_id, {})
    return bind(cls(settings), cfg)
