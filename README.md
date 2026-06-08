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
- **medic_iabotdb** — WaybackMedic's edits to the IABot DB (archive add/modify/
  delete + URL status changes), from medic's `iabget.done` logs on host `rabbit`
  (global; 2017→).
- **medic_enwiki** — WaybackMedic's dead-link repairs on English Wikipedia (links
  moved to new URLs, archive URLs added, url-status flips, pages edited), computed
  from each `meta/<name>.<range>/` project's logs on host `rabbit` (global; 2015→).
- **arcstat** — archive-URL *inventory* across wikis: per-site snapshots of how many
  wayback/alt-archive/archive.is/webcite links, pages-with-each, and archive.org media
  items each Wikipedia holds. The first **gauge** source (levels, not work-per-period),
  from `quepasa:~/toolforge/arcstat/db/master.db` (per-wiki; 2019→).
- **numberofurl** — external-URL *inventory* across **all** Wikimedia wikis (~850):
  per-site total/unique/pages-with for all-external, Internet Archive, Wayback,
  Archive.today, WebCite. Also a **gauge**, read from the Commons tabular page
  `Data:Wikipedia_statistics/exturls.tab` (revisions backfill + acre's local
  `datau.tab` going forward); monthly since Oct 2025.

---

## Contents
- [Architecture](#architecture)
- [Repo layout](#repo-layout)
- [Data model](#data-model)
- [API (v1)](#api-v1)
- [Front-end (dashboard)](#front-end-dashboard)
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
  api/read.py             public read API (catalog, /series, /events, /grid)
  api/write.py            POST /events, registration, /sources/<slug>/rebuild-rollups
  web.py                  dashboard route ("/") — renders the shell + injects i18n
  i18n.py                 message-catalog loader (messages/<lang>.json)
  messages/en.json        all UI + data-label text (the only place strings live)
  templates/dashboard.html  dashboard shell (bootstraps catalog, loads tss.js + uPlot)
  static/tss.js,tss.css   data-driven dashboard (control panel, grids, uPlot charts)
  static/uPlot.*          vendored chart lib (no CDN at runtime)
  requirements.txt        pinned to the Toolforge python3.11 runtime
loaders/                  (clients/adapters; stdlib only)
  tss_http.py             shared hardened POST-with-retry helper
  tss_token.py            token resolution (--token / --token-file / env / ~/.tss_token)
  iabw_parse.py           shared IABW db-file parser (heals NUL corruption)
  backfill_iabw.py        one-time IABW history load (run on acre)
  upload_outbox_iabw.py   live IABW uploader: byte-offset tail of db files (acre cron)
  pull_booksup.py         daily BooksUp pull from its JSONL (Toolforge job)
  pull_iabotapi.py        IABot API pull: paced backfill + daily (Toolforge job)
  pull_medic_iabotdb.py   WaybackMedic IABot-DB log parser (--local-dir, dry-run, checkpointed)
  sync_medic_iabotdb.sh   acre cron: rsync iabget.done from rabbit + ingest new projects
  pull_medic_enwiki.py    WaybackMedic enwiki-repair stats: remote-ssh compute on rabbit
                          (no file transfer), dry-run + --since-days, checkpointed
  pull_arcstat.py         archive-URL inventory (GAUGE) from quepasa's master.db:
                          posts deferred + triggers gauge rebuild; dry-run
  pull_numberofurl.py     external-URL inventory (GAUGE) from the Commons .tab page:
                          revisions backfill + local datau.tab forward; dry-run
  tss_wiki.py             MediaWiki API reads for loaders (WMF good citizen): policy
                          User-Agent + escalating maxlag (ported from bup's wiki.py)
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

**Flows vs gauges.** A metric's `default_agg` decides how it consolidates when rolling
up over time. Most sources are **flows** (`sum`) — work done per period, additive
(links added, pages edited). `arcstat` is a **gauge** (`last`) — an inventory *level*
("as of date D, site X holds N links"), so across time the rollup keeps the **last**
reading of the period, and the combined `_all` total **sums** each entity's last reading.
Only the set-based `rebuild_source` implements gauges (window functions); the live
per-write `recompute()` is sum-only, so gauge sources load via `?rollup=defer` + rebuild.
avg/max/min/counter aren't implemented yet (`rebuild_source` raises if it sees one).

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
- `GET /catalog` — every source with its metrics + derived `house` (activity/inventory);
  powers the dashboard control panel in one call.
- `GET /grid?source=&metric=&grain=&from=&to=` — all entities × periods in one response
  (the spreadsheet matrix; `/series` is per-entity). `null` cells = no data; the combined
  `_all` row is returned separately.

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

## Front-end (dashboard)

Served at `/` (https://tss.toolforge.org/). A single page, **data-driven** entirely
from the read API — it reads nothing source-specific in code, so new sources/metrics
appear automatically.

- **Two houses**, derived from `value_type`: **Activity** (flow — work by tools/bots)
  vs **Inventory** (gauge — URL state on the wikis). A source is Inventory iff *all*
  its metrics are gauges.
- **Control panel → Go → result below.** Pick house → source → tables (metrics,
  multi-select) → grain (D/M/Y) → display (grid | chart), plus a single-wiki filter and
  a "summary only" (combined `_all`) toggle.
- **Grid** = wiki × period (sortable columns, per-wiki row + a combined row). Flow shows
  a row **Total**; gauge shows **Latest** (a period-sum of a level is meaningless). A
  `null` cell (no data) renders as `·`, distinct from a real `0`. uniq\* gauges show the
  per-wiki-sum caveat.
- **Chart** = uPlot: **bars** for flow, **line** for gauge (combined, or the filtered wiki).
- **i18n / labels:** every string — UI chrome *and* source/metric labels — comes from
  `messages/<lang>.json` via a `t()` lookup (DB label is the fallback). Change a label =
  edit `en.json`; add a language = add `fr.json`/`de.json` (auto-listed). Locale via
  `?lang=`; numbers are locale-formatted. **Permalinks:** the full control-panel state is
  encoded in the URL.
- Backed by `GET /catalog` (control panel) + `GET /grid` (the matrix).

**Not yet:** the dashboard is currently **unauthenticated** (it reads the already-public
API) — gating it behind login/Wikimedia OAuth, and adding `fr`/`de` catalogs, are the
next layers.

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

The medic sources (`medic_iabotdb`, `medic_enwiki`) are loaded from `acre`, so their
tokens live there too (issue on Toolforge, copy down to `~/.tss_token_medic_*` on acre,
mode 600 — same pattern as eventstreams in step 6/7).

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
| `5,20,35,50 * * * *` | `/home/greenc/repos/gh/tss/loaders/upload_outbox_iabw.py --token-file /home/greenc/.tss_token >> /home/greenc/tss_upload.log 2>&1` | Tail new eventstreams rows → POST to TSS (live). Runs 5 min after the IABW `cron-run` cycle. |
| `30 3 * * *` | `/home/greenc/repos/gh/tss/loaders/sync_medic_iabotdb.sh >> /home/greenc/medic_iabotdb.log 2>&1` | rsync new iabget.done from rabbit's local disk + ingest newly-completed medic projects. |
| `45 3 * * *` | `/home/greenc/repos/gh/tss/loaders/pull_medic_enwiki.py --since-days 60 >> /home/greenc/medic_enwiki.log 2>&1` | Compute enwiki-repair stats on rabbit for projects finished in the last 60 days + ingest newly-finished ones. |
| `30 4 * * *` | `/home/greenc/repos/gh/tss/loaders/pull_arcstat.py >> /home/greenc/arcstat.log 2>&1` | Daily: re-post arcstat inventory from quepasa's master.db (deferred) + trigger gauge rebuild. Sites update on their own cron throughout the day, often 1+/day. |
| `0 5 * * *` | `/home/greenc/repos/gh/tss/loaders/pull_numberofurl.py >> /home/greenc/numberofurl.log 2>&1` | Daily check: load the new monthly Commons snapshot from acre's `datau.tab` (no-op if that snapshot date is already loaded) + gauge rebuild. |

```cron
# TSS uploader — drain new eventstreams rows to TSS, 5 min after each cron-run cycle
5,20,35,50 * * * * /home/greenc/repos/gh/tss/loaders/upload_outbox_iabw.py --token-file /home/greenc/.tss_token >> /home/greenc/tss_upload.log 2>&1
# WaybackMedic IABot-DB daily sync (rabbit local disk -> TSS)
30 3 * * * /home/greenc/repos/gh/tss/loaders/sync_medic_iabotdb.sh >> /home/greenc/medic_iabotdb.log 2>&1
# WaybackMedic enwiki-repair daily sync (remote compute on rabbit -> TSS)
45 3 * * * /home/greenc/repos/gh/tss/loaders/pull_medic_enwiki.py --since-days 60 >> /home/greenc/medic_enwiki.log 2>&1
# arcstat archive-URL inventory (gauge) daily sync (quepasa master.db -> TSS)
30 4 * * * /home/greenc/repos/gh/tss/loaders/pull_arcstat.py >> /home/greenc/arcstat.log 2>&1
# numberofurl external-URL inventory (gauge) daily check (Commons .tab -> TSS)
0 5 * * * /home/greenc/repos/gh/tss/loaders/pull_numberofurl.py >> /home/greenc/numberofurl.log 2>&1
```
(On acre the live crontab uses tcsh redirect `>>&`; the lines above are shown in
bash syntax for portability.)
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

### WaybackMedic (medic_iabotdb)

medic's `iabget.done` logs live on host **rabbit** (local disk; `sheep` is a VM on
rabbit and reaches them via a slow shared-folder NFS — do NOT stream through sheep).
The logs total ~1.9 GB / ~4.8M lines, so always work from a **local copy** on acre.

Backfill (one-time), run on acre:
```bash
# copy iabget.done from rabbit's local disk (fast)
mkdir -p /beater/medic_metaimp
rsync -rt --prune-empty-dirs --include='*/' --include='iabget.done' --exclude='*' \
  rabbit:/home/greenc/sharedNFS/medic/metaimp/ /beater/medic_metaimp/
# ALWAYS dry-run first (parse + breakdown, no upload, no state) — verify, then hot-run
./loaders/pull_medic_iabotdb.py --dry-run  --local-dir /beater/medic_metaimp
./loaders/pull_medic_iabotdb.py --backfill --local-dir /beater/medic_metaimp   # ~10 min
# rebuild rollups (Toolforge)
toolforge jobs run rebuild-medic --image python3.11 --mount all --wait \
  --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/rebuild_rollups.py medic_iabotdb'
```
Ongoing: the `sync_medic_iabotdb.sh` acre cron (above) pulls a fresh copy from
rabbit (~1 min), ingests newly-completed projects (the per-project done-set in
`~/.tss_medic_iabotdb.state` — kept off /beater — skips the rest), and removes its
`/beater` scratch dir on exit so nothing stale is left behind. Not
freshness-monitored (sporadic manual batches would false-alarm). After a manual
backfill, `rm -rf /beater/medic_metaimp` when done (or just let the next sync clear it).

### WaybackMedic (medic_enwiki)

A *different* slice of medic from `medic_iabotdb`: the dead-link **repairs on English
Wikipedia**, one `rabbit:/home/greenc/sharedNFS/medic/meta/<name>.<range>/` directory
per project. A project is "finished" exactly when `discovered.orig` exists (the push
script writes it only after edits land on enwiki); that file's mtime (falling back to
`Documentation`'s) is the event date. Five global metrics are computed straight from
each project's logs — the SAME numbers the per-project tcsh `stats` script reports:

| metric | from | available since |
|---|---|---|
| `pages_edited` | `wc -l discovered.orig` | ~2015 |
| `archives_added` | `wc -l newialink` + `wc -l newaltarch` | ~2015 |
| `status_to_live` / `status_to_dead` | `grep -c 'url-status live\|dead' urlchanger` | ~2021 |
| `links_moved` | sum of 5 `syslog` `urlchanger7.1.NN{A,B,I,H,D}` counts (= `stats`'s `convi`; `normal` is `[1-9]B`, excluding `7.1.0B`) | softredir era, ~Nov 2024 |

**File-presence = data-presence:** a metric is emitted only when its source log
exists; an absent log (an older project predating that feature) is NO DATA, not a
zero. `links_moved` reads `syslog`, which contains only *this* project's enwiki moves
— the iabot-system moves (digit-prefixed entries in the per-domain softredir cache)
are logged elsewhere and counted under `medic_iabotdb`, so there's no double-count.
(`stats` itself can be re-derived from `convi`/`nai`/`urlsli`/`urlsdi`/`disci`; we
skip running it because it needs sheep-only softredir rulesets + a per-project arg,
and the oldest projects predate `stats` entirely.)

Compute happens **on rabbit over a single `ssh … bash -s` stream** (the big
`syslog`/`urlchanger` files never cross the network and nothing is copied to /beater);
the adapter receives one compact summary line per project.

Backfill (one-time), run on acre — ALWAYS dry-run first:
```bash
cd /home/greenc/repos/gh/tss
./loaders/pull_medic_enwiki.py --dry-run               # per-year breakdown, no upload (~90s)
./loaders/pull_medic_enwiki.py --backfill              # all finished projects, rollups deferred
# rebuild rollups (Toolforge)
toolforge jobs run rebuild-medic-enwiki --image python3.11 --mount all --wait \
  --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/rebuild_rollups.py medic_enwiki'
```
Ongoing: the `pull_medic_enwiki.py --since-days 60` acre cron (above) recomputes only
recently-finished projects and posts the new ones (per-project done-set in
`~/.tss_medic_enwiki.state` skips the rest). Idempotent (`ext_key = <project>:<metric>`)
and checkpointed, so a crash/re-run re-reads the fast stream and re-sends nothing.
Not freshness-monitored (sporadic manual batches would false-alarm).

### arcstat (the gauge source)

Archive-URL **inventory** (levels, not work-per-period): one line per `(site, reading)`
in `quepasa:~/toolforge/arcstat/db/master.db` —
`<site> <YYYYMMDD> <content_pages> <v1|…|v16>`. The 16 pipe positions map (in order) to
`wayback_links, pages_wayback, altarchive_links, pages_altarchive, archiveis_links,
pages_archiveis, webcite_links, pages_webcite, googlebooks_links, media_texts,
media_audio, media_movies, media_image, media_other, media_texts_paged, media_dark`;
the leading count is `content_pages`. **Presence = data:** older rows have only 15
positions (field 16 `media_dark` was added later) → no `media_dark` event for those,
not a 0. Truncated lines (<15 positions) are skipped + reported.

**Gauge handling — the one thing that's different from every other source.** These are
`value_type=gauge, default_agg=last`. Two aggregation axes:
- across **time** (day→month→year): the **last** reading in the period (never sum — a
  level summed over months is nonsense);
- across **wikis** (the `_all` total): **sum** of each wiki's last reading.

Only `rebuild_source` honors this (`rollup._rebuild_last`, MariaDB window functions);
the live per-write `recompute()` path is **sum-only**. So gauge sources MUST post with
`?rollup=defer` and then rebuild — which `pull_arcstat.py` does automatically. (For an
all-`sum` source the rebuild SQL is byte-identical to the pre-gauge engine, so existing
sources are unaffected — verified by rebuild-and-compare.)

master.db is tiny (~4k lines), so there's **no checkpoint**: each run re-posts every
line (idempotent via `ext_key = <site>:<date>:<metric>`) and rebuilds the whole source.

Load (on acre) — dry-run prints a per-metric summary incl. "current inventory" (sum of
each site's latest reading) to eyeball against the dashboard totals:
```bash
cd /home/greenc/repos/gh/tss
./loaders/pull_arcstat.py --dry-run                 # parse + summary, no upload
./loaders/pull_arcstat.py --no-rebuild              # post (deferred); rebuild via the job:
toolforge jobs run rebuild-arcstat --image python3.11 --mount all --wait \
  --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/rebuild_rollups.py arcstat'
```
Ongoing: the daily acre cron (above) runs `pull_arcstat.py` with no flags — post
(deferred) then trigger the gauge rebuild via the API. Not freshness-monitored.

### numberofurl (gauge, from Commons)

External-URL **inventory** across ~850 Wikimedia wikis — a gauge like arcstat, but the
data is a Commons **tabular** page, `Data:Wikipedia_statistics/exturls.tab` (a JSON
object: `schema.fields` + `data` rows), regenerated ~monthly (the 15th) by the
numberofurl bot on acre, which also drops a local copy at
`~/toolforge/numberofurl/datau.tab`. Metric slugs are the `.tab` field names verbatim.
There's no per-row date — a snapshot's date comes from the `.tab` description
("Last update: …") for the local file, or the revision timestamp when backfilling.
The `total.*` rows are aggregates and are **skipped** (TSS computes `_all`).

Reads of a *main* WMF API (commons.wikimedia.org) require a policy User-Agent or you
get HTTP 403 — handled by `loaders/tss_wiki.py` (policy UA + escalating `maxlag`,
ported from bup's `wiki.py`). The Cloud services (iabot/booksup) don't need this.

**uniq\* caveat (5 metrics).** Per-site `uniq*` = unique-within-that-wiki (correct).
But unique counts **don't sum across wikis** (the same URL recurs on many), so the
combined `_all` for `uniq*` is a *sum of per-wiki uniques* — an overcount (~+40%) vs
true global-dedup. The real global uniques live only in the page's `total.all` row and
are deliberately **not** loaded (too expensive to compute cross-wiki; out of scope).
Read `uniq*` per-site; treat their `_all` as an upper bound. The other 11 metrics
aggregate cleanly (`_all` == the file's `total.all`).

Backfill (one-time), on acre — history is the Commons page's revisions, gated to the
first reliable snapshot (2025-10-18; earlier revisions are setup churn):
```bash
cd /home/greenc/repos/gh/tss
./loaders/pull_numberofurl.py --backfill --dry-run    # lists snapshots + parse check
./loaders/pull_numberofurl.py --backfill --no-rebuild # post all snapshots (deferred)
toolforge jobs run rebuild-numberofurl --image python3.11 --mount all --wait \
  --command '$HOME/www/python/venv/bin/python $HOME/www/python/src/rebuild_rollups.py numberofurl'
```
Ongoing: the daily acre cron runs `pull_numberofurl.py` (no flags) — reads the local
`datau.tab`, and if its snapshot date isn't already in `~/.tss_numberofurl.state`,
posts it (deferred) + triggers the gauge rebuild. So the daily run is a cheap no-op
until the monthly file changes. Not freshness-monitored.

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
tool and acre), `~/.tss_token_booksup` (booksup; tss tool), `~/.tss_token_iabotapi`
(iabotapi; tss tool), `~/.tss_token_medic_iabotdb` / `~/.tss_token_medic_enwiki`
(the medic sources; on acre), `~/.tss_token_arcstat` (arcstat; on acre), and
`~/.tss_token_numberofurl` (numberofurl; on acre), mode 600, never in git. Only the
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
