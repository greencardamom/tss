-- ============================================================================
-- Tarb Stats Server (TSS) — seed data
-- Registers the first two sources (iabotwatch, booksup) and their metrics.
--
-- Re-runnable: every INSERT uses ON DUPLICATE KEY UPDATE, so running this again
-- updates labels/units in place rather than erroring or duplicating.
--
-- API write tokens are NOT set here (never commit secrets). Set api_token_hash
-- out of band once tokens are issued, e.g.:
--   UPDATE source SET api_token_hash = SHA2('<plaintext-token>', 256)
--    WHERE slug = 'iabotwatch';
--
-- Apply after schema.sql:
--   USE `<your-tool-db-prefix>__tss`;
--   SOURCE sql/seed.sql;
-- ============================================================================

SET NAMES utf8mb4;


-- ----------------------------------------------------------------------------
-- Source 1: IABotWatch  (the original EventStream dashboard)
--   Drill-through links resolve a revid to its diff. The {entity} (wiki) ->
--   domain mapping is mostly *.wikipedia.org; non-wikipedia projects (commons,
--   wiktionary, ...) are resolved by the IABW producer before display.
-- ----------------------------------------------------------------------------
INSERT INTO source (slug, name, description, ref_url_tpl)
VALUES (
    'iabotwatch',
    'InternetArchiveBot Dashboard',
    'Wayback/archive.org links added across Wikimedia projects, from EventStreams.',
    'https://{entity}.wikipedia.org/w/index.php?diff={ref_id}'
)
ON DUPLICATE KEY UPDATE
    name        = VALUES(name),
    description = VALUES(description),
    ref_url_tpl = VALUES(ref_url_tpl);

SET @iabw = (SELECT source_id FROM source WHERE slug = 'iabotwatch');

-- The 6 IABW counters (db field order 3..8 in db/YYYY/NNN.txt)
INSERT INTO metric (source_id, slug, label, unit, value_type, default_agg, category) VALUES
    (@iabw, 'iabot_wayback',    'Wayback URLs added by IABot',                 'links', 'count', 'sum', 'IABot'),
    (@iabw, 'iabot_details',    'archive.org/details links added by an IA bot','links', 'count', 'sum', 'IABot'),
    (@iabw, 'other_details',    'archive.org/details links added by other means','links','count','sum', 'Other'),
    (@iabw, 'user_wayback',     'Wayback URLs added by Users',                 'links', 'count', 'sum', 'Users'),
    (@iabw, 'otherbot_wayback', 'Wayback URLs added by other bots',            'links', 'count', 'sum', 'Other bots'),
    (@iabw, 'iabot_sim',        'sim_ books added by IABot',                   'links', 'count', 'sum', 'IABot')
ON DUPLICATE KEY UPDATE
    label       = VALUES(label),
    unit        = VALUES(unit),
    value_type  = VALUES(value_type),
    default_agg = VALUES(default_agg),
    category    = VALUES(category);


-- ----------------------------------------------------------------------------
-- Source 2: BooksUp  (daily pre-aggregated usage stats; no entity, no drill-through)
--   Note: the derived 'urls_added' field is NOT stored as a metric — it is
--   webtool_urls + gadget_urls and is computed on read.
-- ----------------------------------------------------------------------------
INSERT INTO source (slug, name, description, ref_url_tpl)
VALUES (
    'booksup',
    'BooksUp',
    'Daily usage counts for the BooksUp web tool, on-wiki gadget, and API.',
    NULL
)
ON DUPLICATE KEY UPDATE
    name        = VALUES(name),
    description = VALUES(description),
    ref_url_tpl = VALUES(ref_url_tpl);

SET @bup = (SELECT source_id FROM source WHERE slug = 'booksup');

INSERT INTO metric (source_id, slug, label, unit, value_type, default_agg, category) VALUES
    (@bup, 'webtool_edits', 'Web-tool edits',         'edits', 'count', 'sum', 'Web tool'),
    (@bup, 'webtool_urls',  'Web-tool links added',   'links', 'count', 'sum', 'Web tool'),
    (@bup, 'gadget_edits',  'Gadget edits',           'edits', 'count', 'sum', 'Gadget'),
    (@bup, 'gadget_urls',   'Gadget links added',     'links', 'count', 'sum', 'Gadget'),
    (@bup, 'api_page',      'API calls: page',        'calls', 'count', 'sum', 'API'),
    (@bup, 'api_random',    'API calls: random',      'calls', 'count', 'sum', 'API'),
    (@bup, 'api_worklist',  'API calls: worklist',    'calls', 'count', 'sum', 'API'),
    (@bup, 'api_pages',     'API calls: pages',       'calls', 'count', 'sum', 'API')
ON DUPLICATE KEY UPDATE
    label       = VALUES(label),
    unit        = VALUES(unit),
    value_type  = VALUES(value_type),
    default_agg = VALUES(default_agg),
    category    = VALUES(category);
