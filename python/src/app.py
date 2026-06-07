"""Tarb Stats Server (TSS) — WSGI entry point.

Toolforge's python webservice imports the module-level `app` from this file
(located at ~/www/python/src/app.py).
"""
import os
import sys

# Ensure this directory is importable (so `import db`, `from api... ` work
# regardless of how the WSGI server invokes us).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify

from db import close_db
from api.read import bp as read_bp
from api.write import bp as write_bp

API_PREFIX = "/api/v1"


def create_app():
    app = Flask(__name__)
    app.url_map.strict_slashes = False

    app.register_blueprint(read_bp, url_prefix=API_PREFIX)
    app.register_blueprint(write_bp, url_prefix=API_PREFIX)

    @app.get("/")
    def index():
        return jsonify(service="Tarb Stats Server", api=API_PREFIX)

    @app.get(f"{API_PREFIX}/health")
    def health():
        return jsonify(status="ok")

    @app.errorhandler(404)
    def not_found(_e):
        return jsonify(error="not found"), 404

    @app.errorhandler(500)
    def server_error(_e):
        return jsonify(error="internal server error"), 500

    @app.after_request
    def cors(resp):
        # Public read API — allow browser-based readers to fetch series/catalog.
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")
        return resp

    app.teardown_appcontext(close_db)
    return app


app = create_app()


if __name__ == "__main__":
    # Local dev only. On Toolforge the webservice runs this via uwsgi.
    app.run(host="127.0.0.1", port=5000, debug=True)
