# Tarb Stats Server (TSS)

A multi-source time-series data platform on Toolforge. Applications ("sources")
POST raw measurements ("events") to a write API; the server rolls them up into
Day/Month/Year summaries that drive graphs. Old raw events are exported to
Parquet and pruned; rollups are kept forever.

IABotWatch and BooksUp are the first two sources.

## Layout

The repo root is checked out as `~/www` on Toolforge (so the app lands at
`~/www/python/src`, where the python webservice looks for `app`).

```
sql/
  schema.sql        4 tables: source, metric, event (partitioned), rollup
  seed.sql          registers the iabotwatch + booksup sources and their metrics
python/src/
  app.py            WSGI entry (exposes `app`)
  config.py         DB/config (reads ~/replica.my.cnf)
  db.py             per-request ToolsDB connection
  auth.py           bearer-token auth for writes
  rollup.py         recompute Day/Month/Year summaries from events
  api/read.py       public read API (catalog, /series, /events)
  api/write.py      POST /events + optional registration
  requirements.txt  pinned to the Toolforge python3.11 runtime
```

## API (v1)

Base: `https://tss.toolforge.org/api/v1`

Read (public):
- `GET /sources`
- `GET /sources/<slug>/metrics`
- `GET /sources/<slug>/entities?metric=<slug>`
- `GET /series?source=&metric=&entity=&grain=day|month|year&from=&to=`
- `GET /events?source=&metric=&entity=&date=&page=&limit=`  (drill-through)
- `GET /health`

Write (per-source `Authorization: Bearer <token>`):
- `POST /events`  body `{ "events": [ {metric, ts, value, ext_key, entity?, ref_id?, dims?} ] }`

Admin (`Authorization: Bearer <TSS_ADMIN_TOKEN>`, optional — seed.sql does the same):
- `POST /sources`
- `POST /sources/<slug>/metrics`

## Deploy on Toolforge

```bash
become tss
# one-time: clone the repo so its root IS ~/www (app -> ~/www/python/src)
git clone https://github.com/greencardamom/tss ~/www

# 1. create the database + load schema/seed
sql tools    # opens the ToolsDB client for this tool
#   CREATE DATABASE `<dbuser>__tss` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
#   USE `<dbuser>__tss`;
#   SOURCE ~/www/sql/schema.sql;
#   SOURCE ~/www/sql/seed.sql;

# 2. build the venv in the python3.11 image (NOT on the bastion: it is py3.13,
#    the runtime is py3.11 -- build/verify venvs inside the runtime image)
toolforge jobs run venvbuild --image python3.11 --mount all --wait \
  --command 'python3 -m venv $HOME/www/python/venv && $HOME/www/python/venv/bin/pip install --no-cache-dir -r $HOME/www/python/src/requirements.txt'

# 3. issue a write token for a source (store only its hash)
sql tools
#   UPDATE source SET api_token_hash = SHA2('<plaintext-token>', 256) WHERE slug='iabotwatch';

# 4. start the webservice
webservice python3.11 start
webservice status
```

`config.py` derives the DB name as `<replica.my.cnf user>__tss`; override with
`TSS_DB_NAME`. The optional admin endpoints require `TSS_ADMIN_TOKEN` in the
environment.

## Rebuild rollups

Run as a python3.11 job (a full rebuild can exceed the HTTP timeout, and the
venv python only works inside the 3.11 image):

```bash
toolforge jobs run rebuild-iabw --image python3.11 --mount all --wait \
  --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/rebuild_rollups.py iabotwatch'
```

## Redeploy

After the one-time clone above, `./tsssave.sh "message"` commits, pushes to
GitHub, then pulls + restarts the webservice on Toolforge.
