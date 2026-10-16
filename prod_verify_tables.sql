-- ============================================================================
--  NEW_BOT — PROD VERIFICATION SCRIPT
-- ----------------------------------------------------------------------------
--  Run AFTER prod_create_tables.sql (and again after loading the config data).
--  Read-only. Creates nothing, changes nothing.
--
--  USAGE:
--    mysql -h <PROD_IP> -u <user> -p GHN_PROD_BAK -t < prod_verify_tables.sql
--
--  The -t flag prints results as boxed tables (much easier to read).
-- ============================================================================


-- ----------------------------------------------------------------------------
-- CHECK 1 — do all 34 bot tables exist?
--           Any row showing "!! MISSING !!" means prod_create_tables.sql
--           did not finish. Re-run it; it is safe to run again.
-- ----------------------------------------------------------------------------
SELECT '=== CHECK 1: BOT TABLES (expect 34 present, 0 missing) ===' AS `Section`;

SELECT
    e.table_name                              AS `Table`,
    e.bucket                                  AS `Data verdict`,
    IF(t.TABLE_NAME IS NULL,
       '!! MISSING !!', 'present')            AS `Status`
FROM (
      SELECT 'new_bot_hotels'            AS table_name, 'COPY DATA'    AS bucket UNION ALL
      SELECT 'new_bot_properties',            'COPY DATA'    UNION ALL
      SELECT 'new_bot_business_config',       'COPY DATA'    UNION ALL
      SELECT 'new_bot_services',              'COPY DATA'    UNION ALL
      SELECT 'new_bot_prompt_registry',       'COPY DATA'    UNION ALL
      SELECT 'new_bot_kb_files',              'COPY DATA'    UNION ALL
      SELECT 'new_bot_kb_books',              'COPY DATA'    UNION ALL
      SELECT 'new_bot_kb_pages',              'COPY DATA'    UNION ALL
      SELECT 'new_bot_kb_book_pages',         'COPY DATA'    UNION ALL
      SELECT 'bot_hotel_images_scraper',      'COPY DATA'    UNION ALL
      SELECT 'new_bot_location_intros',       'COPY DATA'    UNION ALL
      SELECT 'new_bot_widget_deployments',    'COPY DATA'    UNION ALL
      SELECT 'new_bot_capabilities',          'empty in dev' UNION ALL
      SELECT 'new_bot_intents',               'empty in dev' UNION ALL
      SELECT 'new_bot_restaurants',           'empty in dev' UNION ALL
      SELECT 'new_bot_menu_items',            'empty in dev' UNION ALL
      SELECT 'new_bot_guests',                'NO DATA'      UNION ALL
      SELECT 'new_bot_bookings',              'NO DATA'      UNION ALL
      SELECT 'new_bot_orders',                'NO DATA'      UNION ALL
      SELECT 'new_bot_order_items',           'NO DATA'      UNION ALL
      SELECT 'new_bot_conversations',         'NO DATA'      UNION ALL
      SELECT 'new_bot_messages',              'NO DATA'      UNION ALL
      SELECT 'new_bot_tickets',               'NO DATA'      UNION ALL
      SELECT 'new_bot_sessions',              'NO DATA'      UNION ALL
      SELECT 'new_bot_ird_guest_orders',      'NO DATA'      UNION ALL
      SELECT 'new_bot_log_messages',          'NO DATA'      UNION ALL
      SELECT 'new_bot_log_chips',             'NO DATA'      UNION ALL
      SELECT 'new_bot_log_routing',           'NO DATA'      UNION ALL
      SELECT 'new_bot_log_backend',           'NO DATA'      UNION ALL
      SELECT 'new_bot_log_llm',               'NO DATA'      UNION ALL
      SELECT 'new_bot_log_tickets',           'NO DATA'      UNION ALL
      SELECT 'new_bot_log_errors',            'NO DATA'      UNION ALL
      SELECT 'new_bot_log_llm_daily',         'NO DATA'      UNION ALL
      SELECT 'scrape_jobs',                   'NO DATA'
     ) e
LEFT JOIN information_schema.TABLES t
       ON t.TABLE_SCHEMA = DATABASE()
      AND t.TABLE_NAME   = e.table_name
ORDER BY (t.TABLE_NAME IS NULL) DESC, e.bucket, e.table_name;


-- ----------------------------------------------------------------------------
-- CHECK 2 — headline count. Must be 34.
-- ----------------------------------------------------------------------------
SELECT '=== CHECK 2: COUNT (expect 34) ===' AS `Section`;

SELECT COUNT(*) AS `Bot tables present`,
       IF(COUNT(*) = 34, 'OK', 'INCOMPLETE - re-run prod_create_tables.sql') AS `Verdict`
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN (
      'new_bot_hotels','new_bot_properties','new_bot_business_config','new_bot_services',
      'new_bot_prompt_registry','new_bot_kb_files','new_bot_kb_books','new_bot_kb_pages',
      'new_bot_kb_book_pages','bot_hotel_images_scraper','new_bot_location_intros',
      'new_bot_widget_deployments','new_bot_capabilities','new_bot_intents',
      'new_bot_restaurants','new_bot_menu_items','new_bot_guests','new_bot_bookings',
      'new_bot_orders','new_bot_order_items','new_bot_conversations','new_bot_messages',
      'new_bot_tickets','new_bot_sessions','new_bot_ird_guest_orders',
      'new_bot_log_messages','new_bot_log_chips','new_bot_log_routing','new_bot_log_backend',
      'new_bot_log_llm','new_bot_log_tickets','new_bot_log_errors','new_bot_log_llm_daily',
      'scrape_jobs');


-- ----------------------------------------------------------------------------
-- CHECK 3 — do the 9 legacy GHN tables the bot depends on exist and resolve?
--           These are NOT created by us. If any is missing, the bot loses
--           ticketing / guest lookup / chat history. All 9 must say 'present'.
-- ----------------------------------------------------------------------------
SELECT '=== CHECK 3: LEGACY GHN DEPENDENCIES (expect 9 present) ===' AS `Section`;

SELECT
    CONCAT(e.db, '.', e.tbl)                        AS `Legacy table`,
    e.usage_note                                    AS `Bot uses it for`,
    e.access                                        AS `Access`,
    IF(t.TABLE_NAME IS NULL, '!! MISSING !!', 'present') AS `Status`
FROM (
   SELECT DATABASE() AS db, 'KEBO_ENGAGE_SESSIONS'  AS tbl, 'READ+WRITE' AS access,
          'Chat history the dashboard renders + agent-handoff ownership' AS usage_note UNION ALL
   SELECT DATABASE(), 'GHN_CHAT_BOT_CUSTOMER_DETAILS','READ+WRITE',
          'Lead identity per session so the dashboard shows a guest name' UNION ALL
   SELECT DATABASE(), 'GHN_CHATBOT_FORMS',           'READ+WRITE',
          'In-chat form definitions synced from new_bot_services.form_config' UNION ALL
   SELECT DATABASE(), 'GHN_OPERATION_TICKETS',       'READ',
          'Finding a guest existing open tickets (dedupe / edit)' UNION ALL
   SELECT DATABASE(), 'DEPARTMENT_STAFF_CONFIG',     'READ',
          'Departments per entity + which manager to assign' UNION ALL
   SELECT DATABASE(), 'DEPARTMENT_MASTER',           'READ',
          'Department id to name' UNION ALL
   SELECT DATABASE(), 'ORGANIZATION_OUTLETS',        'READ',
          'Outlet list per FMS entity' UNION ALL
   SELECT 'GHN_FF_PROD', 'GHN_FF_GUEST_INFO',        'READ',
          'Guest name, room, stay dates -> journey phase' UNION ALL
   SELECT 'GHN_FEEDBACK_RESULTS','GHN_SURVEY_FMS_RI_ENTITY_MAPPING','READ',
          'FMS entity id to RI entity id (every department lookup joins this)'
   ) e
LEFT JOIN information_schema.TABLES t
       ON t.TABLE_SCHEMA = e.db
      AND t.TABLE_NAME   = e.tbl
ORDER BY (t.TABLE_NAME IS NULL) DESC, e.db, e.tbl;


-- ----------------------------------------------------------------------------
-- CHECK 4 — KEBO_ENGAGE_SESSIONS must have its 4 non-ORM columns.
--           If this returns 0, the app created a crippled copy because it was
--           pointed at a database where the real table did not exist.
-- ----------------------------------------------------------------------------
SELECT '=== CHECK 4: KEBO_ENGAGE_SESSIONS integrity (expect 4) ===' AS `Section`;

SELECT COUNT(*) AS `Extra cols found`,
       IF(COUNT(*) = 4, 'OK - real GHN table',
          '!! WRONG TABLE - app built a crippled copy, check DB name !!') AS `Verdict`
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME   = 'KEBO_ENGAGE_SESSIONS'
  AND COLUMN_NAME IN ('CACHED_TOKENS','INPUT_TOKENS','OUTPUT_TOKENS','auto_load_form');


-- ----------------------------------------------------------------------------
-- CHECK 5 — row counts after you load the config data.
--           Before the copy every number is 0. After the copy, the
--           "COPY DATA" tables must match the dev counts in the last column.
-- ----------------------------------------------------------------------------
SELECT '=== CHECK 5: ROW COUNTS (compare vs dev) ===' AS `Section`;

SELECT 'new_bot_hotels'           AS `Table`, COUNT(*) AS `Prod rows`, 7    AS `Dev rows` FROM new_bot_hotels
UNION ALL SELECT 'new_bot_properties',        COUNT(*), 74   FROM new_bot_properties
UNION ALL SELECT 'new_bot_business_config',   COUNT(*), 65   FROM new_bot_business_config
UNION ALL SELECT 'new_bot_services',          COUNT(*), 26   FROM new_bot_services
UNION ALL SELECT 'new_bot_prompt_registry',   COUNT(*), 76   FROM new_bot_prompt_registry
UNION ALL SELECT 'new_bot_kb_files',          COUNT(*), 73   FROM new_bot_kb_files
UNION ALL SELECT 'new_bot_kb_books',          COUNT(*), 1557 FROM new_bot_kb_books
UNION ALL SELECT 'new_bot_kb_pages',          COUNT(*), 2221 FROM new_bot_kb_pages
UNION ALL SELECT 'new_bot_kb_book_pages',     COUNT(*), 3521 FROM new_bot_kb_book_pages
UNION ALL SELECT 'bot_hotel_images_scraper',  COUNT(*), 2547 FROM bot_hotel_images_scraper
UNION ALL SELECT 'new_bot_location_intros',   COUNT(*), 45   FROM new_bot_location_intros
UNION ALL SELECT 'new_bot_widget_deployments',COUNT(*), 2    FROM new_bot_widget_deployments;


-- ----------------------------------------------------------------------------
-- CHECK 6 — hotel IDs must be PRESERVED from dev, not renumbered 1-7.
--           Expect exactly: 54, 65, 66, 67, 68, 69, 70
--           (minus any tenant you deliberately chose not to migrate).
--           If you see 1,2,3... the copy did not preserve primary keys and
--           every property / prompt / KB page is attached to the wrong hotel.
-- ----------------------------------------------------------------------------
SELECT '=== CHECK 6: HOTEL IDS (expect 54,65,66,67,68,69,70 - NOT 1-7) ===' AS `Section`;

SELECT id, code, name, city, is_active,
       IF(id < 50, '!! RENUMBERED - PKs were not preserved !!', 'ok') AS `Verdict`
FROM new_bot_hotels
ORDER BY id;


-- ----------------------------------------------------------------------------
-- CHECK 7 — orphan check. Every number here must be 0.
--           A non-zero row means child rows point at a hotel/property that
--           does not exist, i.e. the ids shifted during the copy.
-- ----------------------------------------------------------------------------
SELECT '=== CHECK 7: ORPHANS (every count must be 0) ===' AS `Section`;

SELECT 'properties with no hotel' AS `Orphan check`, COUNT(*) AS `Count`
  FROM new_bot_properties p LEFT JOIN new_bot_hotels h ON h.id = p.hotel_id WHERE h.id IS NULL
UNION ALL
SELECT 'services with no hotel', COUNT(*)
  FROM new_bot_services s LEFT JOIN new_bot_hotels h ON h.id = s.hotel_id WHERE h.id IS NULL
UNION ALL
SELECT 'kb_books with no hotel', COUNT(*)
  FROM new_bot_kb_books b LEFT JOIN new_bot_hotels h ON h.id = b.hotel_id WHERE h.id IS NULL
UNION ALL
SELECT 'kb_books with a dead property', COUNT(*)
  FROM new_bot_kb_books b LEFT JOIN new_bot_properties p ON p.id = b.property_id
 WHERE b.property_id IS NOT NULL AND p.id IS NULL
UNION ALL
SELECT 'kb_pages with no hotel', COUNT(*)
  FROM new_bot_kb_pages g LEFT JOIN new_bot_hotels h ON h.id = g.hotel_id WHERE h.id IS NULL
UNION ALL
SELECT 'kb_pages with a dead property', COUNT(*)
  FROM new_bot_kb_pages g LEFT JOIN new_bot_properties p ON p.id = g.property_id
 WHERE g.property_id IS NOT NULL AND p.id IS NULL
UNION ALL
SELECT 'book_pages with a dead book', COUNT(*)
  FROM new_bot_kb_book_pages bp LEFT JOIN new_bot_kb_books b ON b.id = bp.book_id WHERE b.id IS NULL
UNION ALL
SELECT 'book_pages with a dead page', COUNT(*)
  FROM new_bot_kb_book_pages bp LEFT JOIN new_bot_kb_pages g ON g.id = bp.page_id WHERE g.id IS NULL
UNION ALL
SELECT 'images with no hotel', COUNT(*)
  FROM bot_hotel_images_scraper i LEFT JOIN new_bot_hotels h ON h.id = i.hotel_id WHERE h.id IS NULL
UNION ALL
SELECT 'prompt_registry with a dead hotel', COUNT(*)
  FROM new_bot_prompt_registry r LEFT JOIN new_bot_hotels h ON h.id = r.hotel_id
 WHERE r.hotel_id IS NOT NULL AND h.id IS NULL
UNION ALL
SELECT 'business_config with no hotel', COUNT(*)
  FROM new_bot_business_config c LEFT JOIN new_bot_hotels h ON h.id = c.hotel_id WHERE h.id IS NULL
UNION ALL
SELECT 'location_intros with a dead hotel', COUNT(*)
  FROM new_bot_location_intros l LEFT JOIN new_bot_hotels h ON h.id = l.hotel_id WHERE h.id IS NULL;


-- ----------------------------------------------------------------------------
-- CHECK 8 — widget origins must NOT still point at dev.
-- ----------------------------------------------------------------------------
SELECT '=== CHECK 8: WIDGET ORIGINS (must be prod domains) ===' AS `Section`;

SELECT widget_key, hotel_code, bot_name,
       CAST(allowed_origins AS CHAR) AS `allowed_origins`
FROM new_bot_widget_deployments;

-- ============================================================================
--  END
-- ============================================================================
