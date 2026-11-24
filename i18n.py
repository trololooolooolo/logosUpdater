"""i18n.py — translation layer for LogosUpdater.

Self-contained translation machinery:
- Flat YAML catalogs under a `lang/` directory (one file per language: `lang/<code>.yaml`).
- `en` is the canonical/default catalog and is always loaded as the fallback.
- Lookup order: active language -> `en` -> the key itself (so a missing key NEVER crashes).
- Only the UI and user-facing HTTP errors are translated; logs stay English.

Usage (module-level convenience API)::

    import i18n
    i18n.load("pl")                 # set active language (caches en as fallback)
    i18n.t("btn.change")            # -> "Zmień"  (falls back to en, then the key)
    i18n.t("gallery.title_ecc", ecc="E2")   # placeholder formatting

The server builds the client-side translation object from ``i18n.catalog()`` and
injects it into the HTML as ``const T = {...}``; client JS then uses ``T.key``.

Only the Python standard library and PyYAML are required.
"""

from __future__ import annotations

import os
from typing import Optional

import yaml

# Directory that holds the YAML catalogs. Defaults to `lang/` next to this module,
# but can be overridden via the LOGOS_LANG_DIR env var or I18n(lang_dir=...).
_DEFAULT_LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")

# Canonical fallback language. Its catalog is always loaded and merged underneath
# whatever language is active.
FALLBACK_LANG = "en"


class I18n:
    """Loads flat YAML catalogs and resolves translation keys with fallback.

    Lookup order for any key: active catalog -> `en` catalog -> the key string itself.
    """

    def __init__(self, lang_dir: Optional[str] = None, lang_code: str = FALLBACK_LANG):
        self.lang_dir = lang_dir or os.environ.get("LOGOS_LANG_DIR") or _DEFAULT_LANG_DIR
        self._cache: dict[str, dict] = {}     # lang_code -> flat catalog
        self.active_lang = FALLBACK_LANG
        self.active: dict = {}
        # Always have the fallback available, then activate the requested language.
        self._fallback = self._load_file(FALLBACK_LANG)
        self.load(lang_code)

    # ------------------------------------------------------------------ loading
    def _path(self, lang_code: str) -> str:
        return os.path.join(self.lang_dir, f"{lang_code}.yaml")

    def _load_file(self, lang_code: str) -> dict:
        """Load and cache a single catalog file. Missing/empty -> {} (never raises)."""
        if lang_code in self._cache:
            return self._cache[lang_code]
        catalog: dict = {}
        path = self._path(lang_code)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                # Coerce keys/values to str so the catalog is uniformly flat text.
                catalog = {str(k): ("" if v is None else str(v)) for k, v in data.items()}
        except FileNotFoundError:
            catalog = {}
        except yaml.YAMLError:
            catalog = {}
        self._cache[lang_code] = catalog
        return catalog

    def load(self, lang_code: str) -> "I18n":
        """Set the active language. `en` stays cached as the fallback."""
        lang_code = (lang_code or FALLBACK_LANG).strip()
        self.active_lang = lang_code
        self.active = self._load_file(lang_code)
        # Make sure the fallback is loaded (in case lang_dir changed at runtime).
        if not self._fallback:
            self._fallback = self._load_file(FALLBACK_LANG)
        return self

    # ------------------------------------------------------------------ lookup
    def t(self, key: str, **kwargs) -> str:
        """Return the translation for `key`, falling back active -> en -> key.

        If kwargs are given, `.format(**kwargs)` is applied. Formatting is tolerant:
        a missing/extra placeholder never raises — the unformatted string is returned.
        """
        value = self.active.get(key)
        if value is None:
            value = self._fallback.get(key)
        if value is None:
            value = key
        if kwargs:
            try:
                value = value.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                # Tolerate missing/mismatched placeholders: return as-is, no crash.
                pass
        return value

    # ------------------------------------------------------------------ helpers
    def catalog(self, lang_code: Optional[str] = None) -> dict:
        """Return the merged (en <- lang) flat dict for the given (or active) language.

        Used by the server to build the injected `const T` object. `en` provides the
        complete key set; the requested language overrides whatever it defines.
        """
        if lang_code is None:
            lang_code = self.active_lang
        lang_catalog = self._load_file(lang_code)
        merged = dict(self._fallback)   # start from canonical en
        merged.update(lang_catalog)     # override with the language's own values
        return merged

    def available_languages(self) -> list[str]:
        """List language codes for which a `lang/<code>.yaml` file exists."""
        try:
            names = os.listdir(self.lang_dir)
        except OSError:
            return []
        langs = sorted(
            os.path.splitext(n)[0]
            for n in names
            if n.endswith(".yaml") and not n.startswith(".")
        )
        return langs


# --------------------------------------------------------------- module-level API
# A default singleton so callers can `import i18n; i18n.t(...)` without wiring an
# instance. The server may instead construct its own I18n(lang_dir=..., lang_code=...).
_default = I18n()


def load(lang_code: str) -> I18n:
    return _default.load(lang_code)


def t(key: str, **kwargs) -> str:
    return _default.t(key, **kwargs)


def catalog(lang_code: Optional[str] = None) -> dict:
    return _default.catalog(lang_code)


def available_languages() -> list[str]:
    return _default.available_languages()


def get_default() -> I18n:
    return _default


# ------------------------------------------------------------------- self-test
if __name__ == "__main__":
    i = I18n()

    langs = i.available_languages()
    print("available languages:", langs)
    assert "en" in langs, "en.yaml must exist"
    assert "pl" in langs, "pl.yaml must exist"

    # Active language switching + Polish lookup.
    i.load("pl")
    assert i.t("btn.change") == "Zmień", f"expected Polish, got {i.t('btn.change')!r}"

    # Brand stays untranslated.
    assert i.t("app.title") == "LogosUpdater"

    # Unknown key returns the key itself (never crashes).
    assert i.t("totally.unknown.key") == "totally.unknown.key"

    # Fallback to en when pl lacks a key: temporarily check a key only added to en
    # is still resolvable via fallback (here we rely on shared key sets, so instead
    # verify the merged catalog has the full en key set).
    merged_pl = i.catalog("pl")
    en = i.catalog("en")
    assert set(en).issubset(set(merged_pl)), "merged pl must contain all en keys"

    # Placeholder formatting.
    out = i.t("gallery.title_ecc", ecc="E2")
    assert out == "Galeria (ECC: E2)", f"got {out!r}"

    # format() tolerance: kwargs for a key with no placeholder must not raise;
    # and a key WITH a placeholder called without it returns the raw string.
    assert i.t("btn.change", unused="x") == "Zmień"
    raw = i.t("gallery.title_ecc")  # no ecc kwarg -> no .format() applied
    assert raw == "Galeria (ECC: {ecc})", f"got {raw!r}"

    # Ad-hoc demo key formatting requested in the spec.
    i_en = I18n(lang_code="en")
    # x.greet isn't a shipped key, so it falls back to the key string; with kwargs it
    # still must not crash (no placeholder in the key -> returned unchanged).
    assert i_en.t("x.greet", name="Bob") == "x.greet"

    # en/pl key sets must be identical (catalog parity check).
    en_keys = set(en)
    pl_file_keys = set(i._load_file("pl"))
    only_en = sorted(en_keys - pl_file_keys)
    only_pl = sorted(pl_file_keys - en_keys)
    assert not only_en, f"keys missing from pl.yaml: {only_en}"
    assert not only_pl, f"keys missing from en.yaml: {only_pl}"
    print(f"en/pl key sets match: {len(en_keys)} keys each")

    print("i18n self-test: OK")
