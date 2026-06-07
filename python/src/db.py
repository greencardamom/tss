"""Per-request ToolsDB connection helper (PyMySQL)."""
import pymysql
import pymysql.cursors
from flask import g

import config


def get_db():
    """Return the connection for the current request, opening one if needed."""
    if "db" not in g:
        g.db = pymysql.connect(
            host=config.DB_HOST,
            port=config.DB_PORT,
            read_default_file=config.REPLICA_CNF,
            database=config.DB_NAME,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()
