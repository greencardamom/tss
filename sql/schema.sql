-- ============================================================================
-- Tarb Stats Server (TSS) — database schema
-- Target: MariaDB (Toolforge ToolsDB)
-- ============================================================================
--
-- A multi-source time-series store. Many applications (sources) POST raw
-- "events" (one measurement: a metric + value at a date, optionally sliced by
-- an entity). The server rolls events up into Day/Month/Year summaries that
-- drive graphs. Old raw events are exported to Parquet and pruned; rollups are
-- kept forever.
--
-- Four tables:
--   source   - dictionary: who sends data
--   metric   - dictionary: what kinds of numbers each source measures
--   event    - the raw measurements (partitioned by year, prunable)
--   rollup   - permanent Day/Month/Year summaries (what graphs read)
--
-- Design notes:
--   * Producers only ever send rows into `event`. The server computes `rollup`.
--   * `entity` is the one fast "slice" column (= wiki for IABW); source-specific
--     extras go in the JSON `dims` column.
--   * Idempotency: every event carries an `ext_key`; re-sending it updates in
--     place (never double-counts).
--   * Values are DECIMAL so future float/gauge metrics work without migration;
--     today's sources are integer counts.
--
-- Apply (on a Toolforge bastion, ToolsDB requires a tool-prefixed db name):
--     CREATE DATABASE `<your-tool-db-prefix>__tss`
--       CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--     USE `<your-tool-db-prefix>__tss`;
--     SOURCE sql/schema.sql;
-- ============================================================================

SET NAMES utf8mb4;


-- ----------------------------------------------------------------------------
-- source — one row per application that sends data
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source (
    source_id      INT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug           VARCHAR(40)  NOT NULL,           -- 'eventstreams', 'booksup', 'iabotapi'
    name           VARCHAR(120) NOT NULL,           -- human-readable title
    description    VARCHAR(255)     NULL,
    -- Drill-through link template. Placeholders {entity} and {ref_id} are filled
    -- per event, e.g. 'https://{entity}.wikipedia.org/w/index.php?diff={ref_id}'
    ref_url_tpl    VARCHAR(255)     NULL,
    -- Store a HASH of the write token, never the plaintext.
    api_token_hash CHAR(64)         NULL,           -- e.g. sha256 hex
    is_active      TINYINT(1)   NOT NULL DEFAULT 1,
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (source_id),
    UNIQUE KEY uk_source_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ----------------------------------------------------------------------------
-- metric — one row per kind of number a source measures
--   IABW: 6 rows (iabot_wayback, user_wayback, ...)
--   BooksUp: webtool_edits, webtool_urls, gadget_*, api_* ...
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric (
    metric_id    INT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_id    INT UNSIGNED NOT NULL,
    slug         VARCHAR(60)  NOT NULL,             -- 'iabot_wayback'
    label        VARCHAR(160) NOT NULL,             -- 'Wayback URLs added by IABot'
    unit         VARCHAR(40)  NOT NULL DEFAULT '',  -- 'links','edits','calls'
    -- count  : events per period (sum them)
    -- gauge  : an absolute reading (average them)
    -- counter: monotonic; graph the rate (delta/time)
    value_type   ENUM('count','gauge','counter') NOT NULL DEFAULT 'count',
    -- how to consolidate when rolling up / zooming out
    default_agg  ENUM('sum','avg','max','min','last') NOT NULL DEFAULT 'sum',
    category     VARCHAR(60)      NULL,             -- UI grouping: 'Web tool','API'
    description  VARCHAR(255)     NULL,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (metric_id),
    UNIQUE KEY uk_metric_slug (source_id, slug),
    CONSTRAINT fk_metric_source
        FOREIGN KEY (source_id) REFERENCES source (source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ----------------------------------------------------------------------------
-- event — the raw measurements (finest grain a source sends)
--   IABW   : one per edit (entity=wiki, ref_id=revid)
--   BooksUp: one per metric per day (entity NULL, ref_id NULL)
--
-- Partitioned by YEAR(ts) so old years can be exported to Parquet and dropped
-- instantly (ALTER TABLE event DROP PARTITION pYYYY).
--
-- NOTE: MariaDB requires the partition column (ts) to be part of every unique
--       key, hence ts appears in the primary and natural keys below. InnoDB
--       does not allow foreign keys on partitioned tables, so source_id /
--       metric_id integrity is enforced at the application layer.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event (
    event_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_id  INT UNSIGNED  NOT NULL,
    metric_id  INT UNSIGNED  NOT NULL,
    entity     VARCHAR(64)       NULL,             -- the slice (wiki); NULL = none
    ts         DATE          NOT NULL,             -- UTC day the measurement covers
    value      DECIMAL(20,4) NOT NULL,             -- usually 1 (IABW) or a daily count
    ref_id     VARCHAR(64)       NULL,             -- e.g. revid, for drill-through
    dims       JSON              NULL,             -- extras, e.g. {"italic":true}
    -- Idempotency key, unique per source. Re-sending the same ext_key updates
    -- the row instead of inserting a duplicate (use INSERT ... ON DUPLICATE
    -- KEY UPDATE). Should be globally unique within a source regardless of ts.
    ext_key    VARCHAR(120)  NOT NULL,
    created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (event_id, ts),
    UNIQUE KEY uk_event_natural (source_id, ext_key, ts),
    KEY ix_event_lookup (source_id, metric_id, entity, ts),  -- drill-through reads
    -- Freshness probe for monitor_tss.py ("last ingest per source"). Required:
    -- created_at is in no other index, so without this the monitor full-scans the
    -- whole table every hour (37M rows -> a 2h hang in Jul 2026). With it, the
    -- per-source "ORDER BY created_at DESC LIMIT 1" is a single backward seek.
    KEY ix_event_freshness (source_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  PARTITION BY RANGE (YEAR(ts)) (
    PARTITION p2020 VALUES LESS THAN (2021),
    PARTITION p2021 VALUES LESS THAN (2022),
    PARTITION p2022 VALUES LESS THAN (2023),
    PARTITION p2023 VALUES LESS THAN (2024),
    PARTITION p2024 VALUES LESS THAN (2025),
    PARTITION p2025 VALUES LESS THAN (2026),
    PARTITION p2026 VALUES LESS THAN (2027),
    PARTITION p2027 VALUES LESS THAN (2028),
    -- Catch-all. A yearly maintenance job should REORGANIZE this into the next
    -- explicit year partition before data for that year arrives.
    PARTITION pmax  VALUES LESS THAN MAXVALUE
  );


-- ----------------------------------------------------------------------------
-- rollup — permanent Day/Month/Year summaries (the graph engine reads this)
--
--   entity = '' means "all entities combined" (the total line). For sources
--   without an entity (BooksUp), only the '' row exists.
--   bucket = the period start date:
--     grain=day   -> the day            (2026-06-06)
--     grain=month -> first of the month (2026-06-01)
--     grain=year  -> first of the year  (2026-01-01)
--   samples = how many events fed this bucket; lets a reader tell a real zero
--             from "no data" (a gap).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rollup (
    source_id  INT UNSIGNED  NOT NULL,
    metric_id  INT UNSIGNED  NOT NULL,
    entity     VARCHAR(64)   NOT NULL DEFAULT '',
    grain      ENUM('day','month','year') NOT NULL,
    bucket     DATE          NOT NULL,
    value      DECIMAL(28,4) NOT NULL DEFAULT 0,   -- aggregated value
    samples    INT UNSIGNED  NOT NULL DEFAULT 0,
    updated_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                      ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (source_id, metric_id, entity, grain, bucket),
    -- Fast "give me this series in date order" scans for graphing:
    KEY ix_rollup_series (source_id, metric_id, grain, entity, bucket),
    CONSTRAINT fk_rollup_metric
        FOREIGN KEY (metric_id) REFERENCES metric (metric_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
