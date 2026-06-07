# Tarb Stats Server (TSS)

A multi-source time-series data platform on Toolforge. Applications ("sources")
POST raw measurements ("events") to a write API; the server rolls them up into
Day / Month / Year summaries that drive graphs. Rollups are kept forever; old
raw events can be archived and pruned (the `event` table is partitioned by year
for this — the export-to-Parquet+drop job is planned, not yet built).

Live URL: `https://tss.toolforge.org` — API under `/api/v1`.

Sources are named after the API/service the data comes from:
- **eventstreams** — Wayback/archive.org links added across Wikimedia projects,
  observed via Wikimedia EventStreams (all actors; imperfect; 2020→), fed live
  from the pipeline running on the host `acre`.
- **booksup** — daily BooksUp usage counts, pulled from BooksUp's published JSONL.
- **iabotapi** — authoritative per-wiki daily IABot activity from IABot's own
  statistics API (bot-only; 2015→).

---

## Contents
- [Architecture](#architecture)
- [Repo layout](#repo-layout)
- [Data model](#data-model)
- [API (v1)](#api-v1)
- [Setup (one-time)](#setup-one-time)
- [Cron jobs / scheduled jobs](#cron-jobs--scheduled-jobs)
- [Operations](#operations)
- [Gotchas](#gotchas)

---

## Architecture

```
                    acre (the EventStream host)                 Toolforge
  ┌─────────────────────────────────────────────┐   ┌──────────────────────────────┐
  │ iabw-stream.csh → transform.awk → db/YYYY/*.txt│  │  tss webservice (Flask, py3.11)│
  │                          │                     │   │     /api/v1  ──────►  ToolsDB │
  │   upload_outbox_iabw.py  │ (cron, every 15m)   │   │                       s…__tss │
  │   tails new rows ────────┼──── POST /events ───┼──►│  (source/metric/event/rollup) │
  └─────────────────────────────────────────────┘   │            ▲                  │
                                                      │            │ POST /events     │
  BooksUp (another Toolforge tool)                    │  pull_booksup.py (daily job)  │
    publishes booksup-stats-<year>.jsonl ─────────────┼──── reads local file ─────────┘
                                                      │  monitor-tss (hourly job) → email on stale
                                                      └──────────────────────────────┘
```

- **Producers only ever POST raw events.** The server computes all rollups.
- **Idempotent ingest:** every event has an `ext_key`; re-POSTing updates in
  place (never double-counts), so retries/replays are safe.
- **Hardened HTTP** (`loaders/tss_http.py`): escalating backoff on 429/503/
  gateway/empty/truncated responses; 4xx is fatal (surfaced, never hammered).
- **Where things run:** the webservice + the BooksUp pull + freshness monitor run
  on Toolforge; the IABW backfill + live uploader run on `acre` (where the db
  files live).

---

## Repo layout

The repo root is checked out as `~/www` on Toolforge (so the app lands at
`~/www/python/src`, where the python webservice looks for `app`).

```
sql/
  schema.sql              4 tables: source, metric, event (partitioned by year), rollup
  seed.sql                registers eventstreams + booksup + iabotapi sources and their metrics
python/src/               (served by the webservice; DB scripts run as py3.11 jobs)
  app.py                  WSGI entry (exposes `app`)
  config.py               DB config (reads ~/replica.my.cnf; db name <dbuser>__tss)
  db.py                   per-request ToolsDB connection
  auth.py                 bearer-token auth for writes
  rollup.py               recompute() per-write + rebuild_source() set-based
  rebuild_rollups.py      standalone full rollup rebuild for a source (run as job)
  monitor_tss.py          data-freshness check; exits non-zero if a source is stale
  api/read.py             public read API (catalog, /series, /events)
  api/write.py            POST /events, registration, /sources/<slug>/rebuild-rollups
  requirements.txt        pinned to the Toolforge python3.11 runtime
loaders/                  (clients/adapters; stdlib only)
  tss_http.py             shared hardened POST-with-retry helper
  tss_token.py            token resolution (--token / --token-file / env / ~/.tss_token)
  iabw_parse.py           shared IABW db-file parser (heals NUL corruption)
  backfill_iabw.py        one-time IABW history load (run on acre)
  upload_outbox_iabw.py   live IABW uploader: byte-offset tail of db files (acre cron)
  pull_booksup.py         daily BooksUp pull from its JSONL (Toolforge job)
  pull_iabotapi.py        IABot API pull: paced backfill + daily (Toolforge job)
tsssave.sh                deploy: commit → push → pull on Toolforge → restart webservice
```

---

## Data model

Four tables (`sql/schema.sql`):

- **`source`** — one row per app (slug, name, `ref_url_tpl` for drill-through,
  `api_token_hash`).
- **`metric`** — one row per kind of number a source measures (slug, label,
  unit, `value_type`, `default_agg`, `category`).
- **`event`** — raw measurements: `source_id, metric_id, entity, ts, value,
  ref_id, dims (JSON), ext_key`. Partitioned by `YEAR(ts)`. `entity` = the one
  fast slice (the wiki, for IABW); `ext_key` is the idempotency key.
- **`rollup`** — permanent Day/Month/Year summaries (`entity=''` = all-combined
  total). `samples` distinguishes a real zero from "no data".

Resolution is Day / Month / Year only (no sub-daily).

---

## API (v1)

Base: `https://tss.toolforge.org/api/v1`

**Read (public):**
- `GET /health`
- `GET /sources`
- `GET /sources/<slug>/metrics`
- `GET /sources/<slug>/entities?metric=<slug>`
- `GET /series?source=&metric=&entity=&grain=day|month|year&from=&to=&fill=none|zero|null`
  — `entity=_all` for the combined total. The graph engine.
- `GET /events?source=&metric=&entity=&date=&page=&limit=` — drill-through to raw rows.

**Write (per-source `Authorization: Bearer <token>`):**
- `POST /events?rollup=defer` — body `{ "events": [ {metric, ts, value, ext_key, entity?, ref_id?, dims?} ] }`.
  `?rollup=defer` skips per-batch rollup recompute (for bulk loads); omit it for
  live updates.
- `POST /sources/<slug>/rebuild-rollups` — rebuild this source's rollups (fast
  for small sources; for a large initial rebuild use the job below instead).

**Admin (`Authorization: Bearer <TSS_ADMIN_TOKEN>`, optional — `seed.sql` does the same):**
- `POST /sources`
- `POST /sources/<slug>/metrics`

---

## Setup (one-time)

Run on a Toolforge bastion unless noted. `<dbuser>` is the `user` in
`~/replica.my.cnf` (e.g. `s57719`); the database name is `<dbuser>__tss`.

### 1. Clone the repo as `~/www`
```bash
become tss
git clone https://github.com/greencardamom/tss ~/www
```

### 2. Create the database + load schema/seed
```bash
DB=$(awk -F'= *' '/^user/{print $2"__tss"}' ~/replica.my.cnf)
MY="mariadb --defaults-file=$HOME/replica.my.cnf -h tools.db.svc.wikimedia.cloud"
$MY -e "CREATE DATABASE IF NOT EXISTS \`$DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
$MY "$DB" < ~/www/sql/schema.sql
$MY "$DB" < ~/www/sql/seed.sql
```

### 3. Build the venv (in the python3.11 image — NOT on the bastion)
```bash
toolforge jobs run venvbuild --image python3.11 --mount all --wait \
  --command 'python3 -m venv $HOME/www/python/venv && $HOME/www/python/venv/bin/pip install --no-cache-dir -r $HOME/www/python/src/requirements.txt'
```

### 4. Issue write tokens (store only the hash in the DB; plaintext in a 600 file)
```bash
# eventstreams — token also needed on acre (see step 7)
TOK=$(python3 -c "import secrets;print(secrets.token_hex(32))")
$MY "$DB" -e "UPDATE source SET api_token_hash='$(printf %s "$TOK"|sha256sum|cut -d' ' -f1)' WHERE slug='eventstreams';"
printf %s "$TOK" > ~/.tss_token; chmod 600 ~/.tss_token

# booksup — used by the pull job on Toolforge
TOK=$(python3 -c "import secrets;print(secrets.token_hex(32))")
$MY "$DB" -e "UPDATE source SET api_token_hash='$(printf %s "$TOK"|sha256sum|cut -d' ' -f1)' WHERE slug='booksup';"
printf %s "$TOK" > ~/.tss_token_booksup; chmod 600 ~/.tss_token_booksup

# iabotapi — used by the pull job on Toolforge
TOK=$(python3 -c "import secrets;print(secrets.token_hex(32))")
$MY "$DB" -e "UPDATE source SET api_token_hash='$(printf %s "$TOK"|sha256sum|cut -d' ' -f1)' WHERE slug='iabotapi';"
printf %s "$TOK" > ~/.tss_token_iabotapi; chmod 600 ~/.tss_token_iabotapi
```

### 5. Start the webservice
```bash
webservice python3.11 start
webservice status
curl https://tss.toolforge.org/api/v1/health     # {"status":"ok"}
```

### 6. Backfill eventstreams history (on `acre`), then rebuild rollups (Toolforge)
```bash
# on acre — copy the eventstreams token down, then load all history
ssh tools 'become tss cat /data/project/tss/.tss_token' > ~/.tss_token; chmod 600 ~/.tss_token
cd /home/greenc/repos/gh/tss
./loaders/backfill_iabw.py            # ~31M events; resumable + idempotent
                                      # (the *_iabw loaders feed the 'eventstreams'
                                      #  source from acre's IABot EventStreams pipeline)

# on Toolforge — one rebuild over the full event table
toolforge jobs run rebuild-es --image python3.11 --mount all --wait \
  --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/rebuild_rollups.py eventstreams'
```

### 7. Hand off to the live IABW uploader (on `acre`), then add its cron
```bash
cd /home/greenc/repos/gh/tss
./loaders/upload_outbox_iabw.py --init     # seed byte-offsets to current file sizes
# re-tail TODAY's file so rows added after the backfill snapshot aren't skipped
# (replace 158 with today's day-of-year):
python3 -c "import json,os;f=os.path.expanduser('~/.tss_outbox_iabw.state');d=json.load(open(f));d['offsets']['2026/158.txt']=0;json.dump(d,open(f,'w'))"
# then add the acre crontab entry from the table below.
```

### 8. BooksUp pull + freshness monitor (Toolforge scheduled jobs)
See the [scheduled jobs](#cron-jobs--scheduled-jobs) table below for the exact
`toolforge jobs run` commands.

---

## Cron jobs / scheduled jobs

Two places run scheduled work: **acre** (the host crontab) and **Toolforge**
(the jobs framework). All of these must exist for a full deployment.

### acre — host crontab

| Schedule | Command | Purpose |
|---|---|---|
| `5,20,35,50 * * * *` | `/home/greenc/repos/gh/tss/loaders/upload_outbox_iabw.py --token-file /home/greenc/.tss_token >> /home/greenc/tss_upload.log 2>&1` | Tail new IABW rows → POST to TSS (live). Runs 5 min after the IABW `cron-run` cycle. |

```cron
# TSS uploader — drain new IABotWatch rows to TSS, 5 min after each cron-run cycle
5,20,35,50 * * * * /home/greenc/repos/gh/tss/loaders/upload_outbox_iabw.py --token-file /home/greenc/.tss_token >> /home/greenc/tss_upload.log 2>&1
```
The uploader holds a PID lockfile so runs never overlap.

### Toolforge — scheduled jobs (`toolforge jobs run`, as the `tss` tool)

| Job | Schedule | `--emails` | Purpose |
|---|---|---|---|
| `pull-booksup` | `0 2 * * *` | `onfailure` | Pull BooksUp's daily JSONL → TSS. Exits non-zero (→ email) if the source file is missing. |
| `pull-iabotapi` | `30 2 * * *` | `onfailure` | Pull the recent 2 months from the IABot API → TSS (live). Paced at 5/min. |
| `monitor-tss` | `@hourly` | `onfailure` | Freshness check; emails if eventstreams stale >6h, or booksup/iabotapi >48h, or a source has no data. |

```bash
# BooksUp daily pull (runs after BooksUp's own midnight stats job)
toolforge jobs run pull-booksup --image python3.11 --mount all \
  --schedule "0 2 * * *" --emails onfailure \
  --command 'python3 $HOME/www/loaders/pull_booksup.py >> $HOME/pull_booksup.log 2>&1'

# IABot API daily pull (recent months, live; paced ~5/min by the adapter)
toolforge jobs run pull-iabotapi --image python3.11 --mount all \
  --schedule "30 2 * * *" --emails onfailure \
  --command 'python3 $HOME/www/loaders/pull_iabotapi.py >> $HOME/pull_iabotapi.log 2>&1'

# Hourly freshness monitor (uses the venv python — it needs pymysql)
toolforge jobs run monitor-tss --image python3.11 --mount all \
  --schedule "@hourly" --emails onfailure \
  --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/monitor_tss.py >> $HOME/monitor_tss.log 2>&1'
```

`--emails onfailure` mails the tool's maintainers (registered in toolsadmin) when
the command exits non-zero. Failure-email recurs each run while a source stays
stale; widen the `monitor-tss` schedule if that's noisy.

To change the eventstreams staleness threshold X, recreate `monitor-tss` with
`… monitor_tss.py --eventstreams-hours N` (and `--booksup-hours` / `--iabotapi-hours`).

**One-off jobs** (not scheduled — note: one-off jobs reject `--timeout`):
- `rebuild-es` / `rebuild-iabotapi` — `--wait` rollup rebuilds; run after a
  backfill or whenever rollups need a full recompute.
- `iabotapi-backfill` — full 2015→present history load (paced ~28 min, resumable
  via `~/.tss_iabotapi.state`, rollups deferred). Run once, then `rebuild-iabotapi`:
  ```bash
  toolforge jobs run iabotapi-backfill --image python3.11 --mount all --emails onfailure \
    --command 'python3 $HOME/www/loaders/pull_iabotapi.py --backfill >> $HOME/iabotapi_backfill.log 2>&1'
  ```

---

## Operations

**Redeploy code:** `./tsssave.sh "message"` — commits, pushes to GitHub, pulls on
Toolforge, restarts the webservice. (One-time clone in setup step 1 first.)

**Rebuild rollups for a source** (set-based, fast; run as a job to avoid the HTTP
timeout on large sources):
```bash
toolforge jobs run rebuild-<src> --image python3.11 --mount all --wait \
  --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/rebuild_rollups.py <slug>'
```

**Check freshness manually:**
```bash
toolforge jobs run monitor-once --image python3.11 --mount all --wait \
  --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/monitor_tss.py'
# (then: toolforge jobs delete monitor-once)
```

**Backfill is resumable:** `backfill_iabw.py` records progress in
`~/.tss_backfill_iabw.state`; re-running skips finished files. The uploader
records byte offsets in `~/.tss_outbox_iabw.state`.

**Tokens:** plaintext lives only in `~/.tss_token` (eventstreams; on both the tss
tool and acre), `~/.tss_token_booksup` (booksup; tss tool), and
`~/.tss_token_iabotapi` (iabotapi; tss tool), mode 600, never in git. Only the
SHA-256 hash is in the DB.

---

## Gotchas

- **Bastion is Python 3.13, the runtime is 3.11.** Anything using the venv
  (venv build, `rebuild_rollups.py`, `monitor_tss.py`) must run as a
  `toolforge jobs run --image python3.11` job, not directly on the bastion
  (the venv python won't import its packages there).
- **`toolforge jobs run --wait` can return before the job's DB transaction
  commits.** When verifying a rebuild, give it a moment / re-check; don't
  `jobs delete` a job you just `--wait`-ed on until it's confirmed done.
- **Adding a new source is data, not a migration:** register it in `source` +
  `metric` (or via the admin API), give it a token, and POST events — no schema
  change. BooksUp was added exactly this way.
