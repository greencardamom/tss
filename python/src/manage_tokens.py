#!/usr/bin/env python3
"""Revoke or rotate a source's write token. Run on Toolforge as the tss tool, from python/src.

  manage_tokens.py list                 slug, active, whether a token is set
  manage_tokens.py revoke SLUG          clear the token hash (writes refused; reads unaffected)
  manage_tokens.py rotate SLUG OUTFILE  new random token; its hash is stored, the token is
                                        written to OUTFILE (mode 600, must not exist) - never printed
"""
import os
import secrets
import sys

from app import create_app
from auth import hash_token
from db import get_db


def main(argv):
    if not argv or argv[0] not in ("list", "revoke", "rotate"):
        sys.exit(__doc__)
    with create_app().app_context():
        db = get_db()
        cur = db.cursor()
        if argv[0] == "list":
            cur.execute("SELECT slug, is_active, api_token_hash IS NOT NULL AS has_token FROM source ORDER BY slug")
            for r in cur.fetchall():
                print("%-24s active=%s token=%s" % (r["slug"], r["is_active"], "set" if r["has_token"] else "none"))
            return
        if len(argv) < 2:
            sys.exit(__doc__)
        slug = argv[1]
        cur.execute("SELECT source_id FROM source WHERE slug = %s", (slug,))
        if not cur.fetchone():
            sys.exit("no such source: %s" % slug)
        if argv[0] == "revoke":
            cur.execute("UPDATE source SET api_token_hash = NULL WHERE slug = %s", (slug,))
            db.commit()
            print("revoked write token for %s" % slug)
            return
        if len(argv) != 3:
            sys.exit(__doc__)
        token = secrets.token_hex(32)
        fd = os.open(argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
        cur.execute("UPDATE source SET api_token_hash = %s WHERE slug = %s", (hash_token(token), slug))
        db.commit()
        print("rotated write token for %s; new token written to %s (copy it to the loader host's ~/.config/tss/)" % (slug, argv[2]))


if __name__ == "__main__":
    main(sys.argv[1:])
