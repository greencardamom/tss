"""Dashboard front-end (server side).

Serves the single dashboard page. All rendering of data (grids, charts) happens
client-side in static/tss.js by calling the public read API (/api/v1/*); this route
only ships the shell + bootstraps the chosen locale's message catalog into the page
(so there's no flash of untranslated text and no extra round-trip).

NOTE: the dashboard currently reads the *public* read API and is itself unauthenticated
-- fine for evaluating the UI. Gating it behind login/OAuth is a separate later layer.
"""
from flask import Blueprint, render_template, request

import i18n

bp = Blueprint("web", __name__)


@bp.get("/")
def dashboard():
    langs = i18n.available() or ["en"]
    lang = request.args.get("lang", "en")
    if lang not in langs:
        lang = "en"
    return render_template("dashboard.html", lang=lang, langs=langs,
                           catalog=i18n.catalog(lang))
