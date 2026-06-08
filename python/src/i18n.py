"""Tiny message-catalog i18n for the TSS dashboard.

All display text -- UI chrome AND data labels (source/metric names) -- lives in
messages/<lang>.json (a flat {message-id: text} map), NOT in code or templates.
So a label change is a JSON edit and a new language is a new file; neither touches
code. `en.json` is the base/fallback; another locale overlays it (missing keys fall
back to English, then -- for data labels -- to the DB label, then the slug, resolved
client-side).

Lookup order for a key: catalog[lang] -> catalog[en]. Locale is chosen by ?lang= in
the URL (default 'en'), so permalinks carry the language too.
"""
import glob
import json
import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages")
_cache = {}


def _load(lang):
    path = os.path.join(_DIR, "%s.json" % lang)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return {}
    return {}


def available():
    """Locale codes with a catalog file (e.g. ['de', 'en', 'fr'])."""
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(_DIR, "*.json")))


def catalog(lang):
    """The merged catalog for `lang`: English base overlaid with the locale's
    overrides. Cached per process (catalogs are static until redeploy)."""
    lang = lang or "en"
    if lang in _cache:
        return _cache[lang]
    merged = dict(_load("en"))
    if lang != "en":
        merged.update(_load(lang))
    _cache[lang] = merged
    return merged
