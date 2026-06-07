#!/usr/bin/env python3
"""rebuild_rollups.py - full rollup rebuild for one source (run on Toolforge).

A set-based rebuild can take minutes on a large source, longer than the web
proxy's request timeout, so run it as a python3.11 JOB rather than via the HTTP
endpoint. Use a job (not the bastion): the venv is 3.11 but the bastion is 3.13,
so running the venv python on the bastion fails to import pymysql.

    toolforge jobs run rebuild-iabw --image python3.11 --mount all --wait \
      --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/rebuild_rollups.py eventstreams'

It connects to ToolsDB the same way the webservice does (config.py).
"""
import sys

import pymysql
import pymysql.cursors

import config
import rollup


def main():
    if len(sys.argv) != 2:
        print("usage: rebuild_rollups.py <source-slug>", file=sys.stderr)
        sys.exit(2)
    slug = sys.argv[1]

    conn = pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        read_default_file=config.REPLICA_CNF,
        database=config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT source_id FROM source WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if not row:
            print(f"unknown source '{slug}'", file=sys.stderr)
            sys.exit(1)
        print(f"rebuilding rollups for '{slug}' ...", flush=True)
        rollup.rebuild_source(cur, row["source_id"])
        conn.commit()
        print("done")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
