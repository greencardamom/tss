-- ============================================================================
-- Tarb Stats Server (TSS) — seed data
-- Registers sources (eventstreams, booksup, iabotapi) and their metrics.
-- Sources are named after the API/service the data comes FROM.
--
-- Re-runnable: every INSERT uses ON DUPLICATE KEY UPDATE, so running this again
-- updates labels/units in place rather than erroring or duplicating.
--
-- NOTE: this registers slugs by INSERT-or-update-on-slug. To RENAME an existing
-- source's slug, do it directly first (UPDATE source SET slug=... WHERE slug=...),
-- otherwise re-running this would create a second row.
--
-- API write tokens are NOT set here (never commit secrets). Set api_token_hash
-- out of band once tokens are issued, e.g.:
--   UPDATE source SET api_token_hash = SHA2('<plaintext-token>', 256)
--    WHERE slug = 'eventstreams';
--
-- Apply after schema.sql:
--   USE `<your-tool-db-prefix>__tss`;
--   SOURCE sql/seed.sql;
-- ============================================================================

SET NAMES utf8mb4;


-- ----------------------------------------------------------------------------
-- Source: eventstreams  (archive-link activity observed via Wikimedia
--   EventStreams: all actors — IABot, users, other bots — imperfect, 2020->).
--   Drill-through resolves a revid to its diff. The {entity} (wiki) -> domain
--   mapping is mostly *.wikipedia.org; non-wikipedia projects (commons,
--   wiktionary, ...) are resolved by the producer before display.
-- ----------------------------------------------------------------------------
INSERT INTO source (slug, name, description, ref_url_tpl)
VALUES (
    'eventstreams',
    'Wikimedia EventStreams',
    'Wayback/archive.org link additions across Wikimedia projects, observed via EventStreams (all actors; since 2020).',
    'https://{entity}.wikipedia.org/w/index.php?diff={ref_id}'
)
ON DUPLICATE KEY UPDATE
    name        = VALUES(name),
    description = VALUES(description),
    ref_url_tpl = VALUES(ref_url_tpl);

SET @es = (SELECT source_id FROM source WHERE slug = 'eventstreams');

-- The 6 counters (db field order 3..8 in the EventStreams pipeline db/YYYY/NNN.txt)
INSERT INTO metric (source_id, slug, label, unit, value_type, default_agg, category) VALUES
    (@es, 'iabot_wayback',    'Wayback URLs added by IABot',                 'links', 'count', 'sum', 'IABot'),
    (@es, 'iabot_details',    'archive.org/details links added by an IA bot','links', 'count', 'sum', 'IABot'),
    (@es, 'other_details',    'archive.org/details links added by other means','links','count','sum', 'Other'),
    (@es, 'user_wayback',     'Wayback URLs added by Users',                 'links', 'count', 'sum', 'Users'),
    (@es, 'otherbot_wayback', 'Wayback URLs added by other bots',            'links', 'count', 'sum', 'Other bots'),
    (@es, 'iabot_sim',        'sim_ books added by IABot',                   'links', 'count', 'sum', 'IABot')
ON DUPLICATE KEY UPDATE
    label       = VALUES(label),
    unit        = VALUES(unit),
    value_type  = VALUES(value_type),
    default_agg = VALUES(default_agg),
    category    = VALUES(category);


-- ----------------------------------------------------------------------------
-- Source: booksup  (daily pre-aggregated usage stats; no entity, no drill-through)
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


-- ----------------------------------------------------------------------------
-- Source: iabotapi  (authoritative IABot activity from its own statistics API,
--   action=statistics; per-wiki daily; bot-only; since 2015). No drill-through.
--   Stored metrics are the API "key" fields EXCEPT the derived TotalEdits /
--   TotalLinks (= sums of the parts), which are computed on read.
-- ----------------------------------------------------------------------------
INSERT INTO source (slug, name, description, ref_url_tpl)
VALUES (
    'iabotapi',
    'InternetArchiveBot API',
    'Authoritative per-wiki daily activity from IABot''s own statistics API (action=statistics); since 2015.',
    NULL
)
ON DUPLICATE KEY UPDATE
    name        = VALUES(name),
    description = VALUES(description),
    ref_url_tpl = VALUES(ref_url_tpl);

SET @iab = (SELECT source_id FROM source WHERE slug = 'iabotapi');

INSERT INTO metric (source_id, slug, label, unit, value_type, default_agg, category) VALUES
    (@iab, 'dead_links',      'Dead links',       'links', 'count', 'sum', 'Links'),
    (@iab, 'live_links',      'Live links',       'links', 'count', 'sum', 'Links'),
    (@iab, 'tag_links',       'Tagged links',     'links', 'count', 'sum', 'Links'),
    (@iab, 'unknown_links',   'Unknown links',    'links', 'count', 'sum', 'Links'),
    (@iab, 'dead_edits',      'Dead-link edits',  'edits', 'count', 'sum', 'Edits'),
    (@iab, 'proactive_edits', 'Proactive edits',  'edits', 'count', 'sum', 'Edits'),
    (@iab, 'reactive_edits',  'Reactive edits',   'edits', 'count', 'sum', 'Edits'),
    (@iab, 'unknown_edits',   'Unknown edits',    'edits', 'count', 'sum', 'Edits')
ON DUPLICATE KEY UPDATE
    label       = VALUES(label),
    unit        = VALUES(unit),
    value_type  = VALUES(value_type),
    default_agg = VALUES(default_agg),
    category    = VALUES(category);


-- ----------------------------------------------------------------------------
-- Source: medic_iabotdb  (WaybackMedic's work in the IABot DB; global, no entity)
--   Per iabget.done line (one IABot-DB update) -> one archive-op metric + one
--   status metric. Bucketed by the project-name date in each line's IMPID.
--   Non-modifyurl/unparseable lines are trapped (logged) by the adapter, not
--   stored. "permadead"/"permalive" = IABot's blacklist(6)/whitelist(7).
-- ----------------------------------------------------------------------------
INSERT INTO source (slug, name, description, ref_url_tpl)
VALUES (
    'medic_iabotdb',
    'WaybackMedic — IABot DB',
    'Per-day counts of WaybackMedic''s updates to the IABot DB (archive add/modify/delete + URL status changes); since 2016.',
    NULL
)
ON DUPLICATE KEY UPDATE
    name        = VALUES(name),
    description = VALUES(description),
    ref_url_tpl = VALUES(ref_url_tpl);

SET @med = (SELECT source_id FROM source WHERE slug = 'medic_iabotdb');

INSERT INTO metric (source_id, slug, label, unit, value_type, default_agg, category) VALUES
    (@med, 'archive_add',    'Archives added',       'urls', 'count', 'sum', 'Archive'),
    (@med, 'archive_modify', 'Archives modified',    'urls', 'count', 'sum', 'Archive'),
    (@med, 'archive_delete', 'Archives deleted',     'urls', 'count', 'sum', 'Archive'),
    (@med, 'status_only',    'Status-only changes',  'urls', 'count', 'sum', 'Archive'),
    (@med, 'set_dead',       'Set dead',             'urls', 'count', 'sum', 'Status'),
    (@med, 'set_alive',      'Set alive',            'urls', 'count', 'sum', 'Status'),
    (@med, 'set_paywall',    'Set paywall',          'urls', 'count', 'sum', 'Status'),
    (@med, 'set_permadead',  'Set permadead',        'urls', 'count', 'sum', 'Status'),
    (@med, 'set_permalive',  'Set permalive',        'urls', 'count', 'sum', 'Status')
ON DUPLICATE KEY UPDATE
    label       = VALUES(label),
    unit        = VALUES(unit),
    value_type  = VALUES(value_type),
    default_agg = VALUES(default_agg),
    category    = VALUES(category);
