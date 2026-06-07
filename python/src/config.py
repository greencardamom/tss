"""TSS configuration.

Reads DB credentials from the Toolforge convention file ~/replica.my.cnf
(the same user/password works for ToolsDB; only the host differs). The tool's
ToolsDB database must be named "<db-user>__tss" per Toolforge rules; we derive
that automatically but it can be overridden with TSS_DB_NAME.
"""
import os
import configparser

REPLICA_CNF = os.path.expanduser(os.environ.get("TSS_DB_CNF", "~/replica.my.cnf"))

DB_HOST = os.environ.get("TSS_DB_HOST", "tools.db.svc.wikimedia.cloud")
DB_PORT = int(os.environ.get("TSS_DB_PORT", "3306"))


def _db_user():
    cp = configparser.ConfigParser()
    try:
        cp.read(REPLICA_CNF)
        return cp.get("client", "user", fallback=None)
    except Exception:
        return None


DB_NAME = os.environ.get("TSS_DB_NAME") or (
    f"{_db_user()}__tss" if _db_user() else "tss"
)

# Largest event batch accepted in a single POST /events. Big batches sharply cut
# per-request round-trip overhead for bulk backfills (~10k events ~= a couple MB,
# well under ToolsDB max_allowed_packet).
MAX_BATCH = int(os.environ.get("TSS_MAX_BATCH", "25000"))

# Optional admin token (plaintext) for the source/metric registration endpoints.
# Registration can also be done directly via sql/seed.sql, so this is optional.
ADMIN_TOKEN = os.environ.get("TSS_ADMIN_TOKEN")
