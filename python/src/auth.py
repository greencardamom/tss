"""Authentication for write endpoints.

Write tokens are per-source. We store only the SHA-256 hash in source.api_token_hash
and look the caller up by hash. The admin token (for registration) is compared in
constant time against config.ADMIN_TOKEN.
"""
import hashlib
import hmac
from functools import wraps

from flask import request, g, jsonify

import config
from db import get_db


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return None


def require_source_token(fn):
    """Resolve the bearer token to an active source; store it on g.source."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _bearer()
        if not token:
            return jsonify(error="missing bearer token"), 401
        cur = get_db().cursor()
        cur.execute(
            "SELECT source_id, slug, ref_url_tpl FROM source "
            "WHERE api_token_hash = %s AND is_active = 1",
            (hash_token(token),),
        )
        source = cur.fetchone()
        if not source:
            return jsonify(error="invalid token"), 401
        g.source = source
        return fn(*args, **kwargs)
    return wrapper


def require_admin_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _bearer()
        if not config.ADMIN_TOKEN or not token or not hmac.compare_digest(
            token, config.ADMIN_TOKEN
        ):
            return jsonify(error="admin token required"), 401
        return fn(*args, **kwargs)
    return wrapper
