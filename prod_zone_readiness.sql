-- ============================================================================
--  ZONE GO-LIVE — PROD READINESS CHECK
-- ----------------------------------------------------------------------------
--  100% READ-ONLY. Creates nothing, changes nothing, deletes nothing.
--  Run on PROD and send the output back.
--
--    mysql -h <PROD_IP> -u <user> -p GHN_PROD_BAK -t < prod_zone_readiness.sql
--
--  (-t prints boxed tables. Safe to run any number of times.)
--
--  WHY: dev's Zone entity ids (6313-6336) are all "... test" entities. Prod
--  needs the REAL Zone entities, and prod's ids will be different numbers.
--  These 7 queries find them and prove the ticketing chain is wired.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- Q1 — Which Zone entities exist in PROD, and what are their ids?
--      Dev has 24 real + 24 "test". Expect ~24 real ones here.
--      NOTE the ids: these replace 6313-6336 in the bot config.
-- ----------------------------------------------------------------------------
SELECT 'Q1: ZONE ENTITIES IN PROD' AS `Section`;

SELECT ID            AS `FMS_Entity_Id`,
       Entity_Name   AS `Entity name`,
       Group_Name    AS `Group`,
       IF(LOWER(TRIM(Entity_Name)) LIKE '%test', '<-- TEST, do not use', 'real') AS `Kind`
FROM GHN_FF_PROD.GHN_FF_ENTITY
WHERE Group_Name LIKE '%Zone Hotels%'
   OR Entity_Name LIKE 'Zone %'
ORDER BY ID;


-- ----------------------------------------------------------------------------
-- Q2 — THE BIG ONE. For every Zone entity: is it mapped FMS->RI, and does it
--      have manager staff configured? This is the exact join the bot runs
--      before it can assign a ticket to a department.
--
--      "RI_Entity_Id" NULL      = no FMS->RI mapping  -> ticketing WILL FAIL
--      "manager_depts" 0        = no staff configured -> ticket gets no owner
-- ----------------------------------------------------------------------------
SELECT 'Q2: MAPPING + STAFF PER ZONE ENTITY' AS `Section`;

SELECT e.ID                                   AS `FMS_Entity_Id`,
       e.Entity_Name                          AS `Entity name`,
       m.RI_Entity_Id                         AS `RI_Entity_Id`,
       COUNT(DISTINCT dsc.DEPARTMENT_ID)      AS `manager_depts`,
       CASE
         WHEN m.RI_Entity_Id IS NULL               THEN 'NO FMS->RI MAPPING'
         WHEN COUNT(DISTINCT dsc.DEPARTMENT_ID)=0  THEN 'NO MANAGER STAFF'
         ELSE 'ok'
       END                                    AS `Verdict`
FROM GHN_FF_PROD.GHN_FF_ENTITY e
LEFT JOIN GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING m
       ON m.FMS_Entity_Id = e.ID
LEFT JOIN DEPARTMENT_STAFF_CONFIG dsc
       ON dsc.ORG_ID = m.RI_Entity_Id AND dsc.MANAGER = 1
WHERE (e.Group_Name LIKE '%Zone Hotels%' OR e.Entity_Name LIKE 'Zone %')
  AND LOWER(TRIM(e.Entity_Name)) NOT LIKE '%test'
GROUP BY e.ID, e.Entity_Name, m.RI_Entity_Id
ORDER BY e.ID;


-- ----------------------------------------------------------------------------
-- Q3 — Which department NAMES are reachable for Zone, exactly as stored?
--      Names are wrapped in [] and their length shown, because dev has a
--      department literally called " Food & Beverage" WITH A LEADING SPACE,
--      which does not string-match the bot config value "Food & Beverage".
--      Check whether prod has the same problem.
-- ----------------------------------------------------------------------------
SELECT 'Q3: DEPARTMENT NAMES REACHABLE BY ZONE (delimited)' AS `Section`;

SELECT DISTINCT
       dm.ID                                  AS `Dept id`,
       CONCAT('[', dm.DEPT_NAME, ']')         AS `Name as stored`,
       CHAR_LENGTH(dm.DEPT_NAME)              AS `Len`,
       IF(dm.DEPT_NAME <> TRIM(dm.DEPT_NAME),
          '<-- HAS STRAY WHITESPACE', '')     AS `Warning`
FROM GHN_FF_PROD.GHN_FF_ENTITY e
JOIN GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING m ON m.FMS_Entity_Id = e.ID
JOIN DEPARTMENT_STAFF_CONFIG dsc ON dsc.ORG_ID = m.RI_Entity_Id AND dsc.MANAGER = 1
JOIN DEPARTMENT_MASTER dm        ON dm.ID = dsc.DEPARTMENT_ID
WHERE (e.Group_Name LIKE '%Zone Hotels%' OR e.Entity_Name LIKE 'Zone %')
  AND LOWER(TRIM(e.Entity_Name)) NOT LIKE '%test'
ORDER BY dm.ID;


-- ----------------------------------------------------------------------------
-- Q4 — Do the 4 departments the bot is allowed to use actually exist in prod?
--      Bot config is: reservations, Food & Beverage, Banquets, complaints
--      "exact_match=0 but trim_match=1" means a whitespace problem like dev has.
-- ----------------------------------------------------------------------------
SELECT 'Q4: ARE THE 4 ALLOWED DEPARTMENTS PRESENT' AS `Section`;

SELECT w.want                                                   AS `Bot config name`,
       SUM(dm.DEPT_NAME = w.want)                               AS `exact_match`,
       SUM(TRIM(dm.DEPT_NAME) = w.want)                         AS `trim_match`,
       GROUP_CONCAT(DISTINCT CONCAT(dm.ID, '=[', dm.DEPT_NAME, ']')
                    ORDER BY dm.ID SEPARATOR '  ')              AS `Found as`
FROM (SELECT 'reservations' AS want UNION ALL
      SELECT 'Food & Beverage'      UNION ALL
      SELECT 'Banquets'             UNION ALL
      SELECT 'complaints') w
LEFT JOIN DEPARTMENT_MASTER dm ON TRIM(dm.DEPT_NAME) = w.want
GROUP BY w.want;


-- ----------------------------------------------------------------------------
-- Q5 — Which widget_id / group_id does Zone use in PROD?
--      Dev uses widget_id=61, group_id=139. Prod will differ.
--      These go into TICKETING_WIDGET_GROUP_MAP_JSON.
-- ----------------------------------------------------------------------------
SELECT 'Q5: WIDGET / GROUP IDS IN USE' AS `Section`;

SELECT widget_id, group_id, COUNT(*) AS `rows`, MAX(timestamp) AS `last seen`
FROM KEBO_ENGAGE_SESSIONS
WHERE timestamp > NOW() - INTERVAL 180 DAY
GROUP BY widget_id, group_id
ORDER BY COUNT(*) DESC
LIMIT 25;


-- ----------------------------------------------------------------------------
-- Q6 — Do the bot tables already exist in prod? (0 = not created yet)
--      Also confirms the 9 legacy tables the bot depends on are present.
-- ----------------------------------------------------------------------------
SELECT 'Q6: TABLE PRESENCE' AS `Section`;

SELECT 'new_bot_* tables created so far' AS `What`,
       COUNT(*)                          AS `Count`,
       '34 when complete'                AS `Expected`
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND (TABLE_NAME LIKE 'new_bot%' OR TABLE_NAME = 'bot_hotel_images_scraper'
       OR TABLE_NAME = 'scrape_jobs')
UNION ALL
SELECT 'legacy GHN tables present', COUNT(*), '9 required'
FROM information_schema.TABLES
WHERE (TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN
        ('KEBO_ENGAGE_SESSIONS','GHN_CHAT_BOT_CUSTOMER_DETAILS','GHN_CHATBOT_FORMS',
         'GHN_OPERATION_TICKETS','DEPARTMENT_STAFF_CONFIG','DEPARTMENT_MASTER',
         'ORGANIZATION_OUTLETS'))
   OR (TABLE_SCHEMA = 'GHN_FF_PROD'          AND TABLE_NAME = 'GHN_FF_GUEST_INFO')
   OR (TABLE_SCHEMA = 'GHN_FEEDBACK_RESULTS' AND TABLE_NAME = 'GHN_SURVEY_FMS_RI_ENTITY_MAPPING');


-- ----------------------------------------------------------------------------
-- Q7 — Sanity: can this login actually READ the two other databases?
--      If either row errors or returns nothing, the grants are missing and
--      guest lookup / department resolution will silently return empty.
-- ----------------------------------------------------------------------------
SELECT 'Q7: CROSS-DATABASE READ ACCESS' AS `Section`;

SELECT 'GHN_FF_PROD.GHN_FF_GUEST_INFO' AS `Table`, COUNT(*) AS `Readable rows`
FROM GHN_FF_PROD.GHN_FF_GUEST_INFO
UNION ALL
SELECT 'GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING', COUNT(*)
FROM GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING;

-- ============================================================================
--  END — send the output back.
-- ============================================================================
