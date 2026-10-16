-- ============================================================================
--  DEV FIX - add escalation for Zone's 4 real departments
-- ----------------------------------------------------------------------------
--  Problem: dev's Zone escalation rows sit on 6 OLD departments (Front Office,
--  Guest Relations, Book a stay, Book a venue, MICE, Call us). Zone's actual 4
--  departments have ZERO escalation rows, so tickets escalate to nobody.
--
--  This adds 96 rows: 24 Zone entities x 4 departments.
--  Values copied exactly from dev's existing escalation rows.
--
--  L1 = 6429 Nidhi     L2 = 6430 Tanay
--  Departments: 1 Banquets, 34 Food & Beverage, 43 reservations, 44 complaints
-- ============================================================================


-- STEP 1 - preview. Must return 96.
SELECT COUNT(*) AS rows_to_insert
FROM (SELECT DISTINCT entity_id FROM GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS
      WHERE entity_id BETWEEN 6313 AND 6336) e
CROSS JOIN (SELECT 1 AS dept_id UNION ALL SELECT 34
            UNION ALL SELECT 43 UNION ALL SELECT 44) d
WHERE NOT EXISTS (
    SELECT 1 FROM GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS x
    WHERE x.entity_id = e.entity_id AND x.department_id = d.dept_id);


-- STEP 2 - note the current max id, so the insert can be undone
SELECT MAX(id) AS max_id_before FROM GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS;


-- STEP 3 - the insert
INSERT INTO GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS
  (entity_id, department_id,
   escalation_1, escalation_2, escalation_3,
   escalation_level_1, escalation_level_2, escalation_level_3,
   is_active, escalation_interval, ticket_auto_assign,
   sla_duration_minutes, sla_data,
   before_sla_default, before_sla_data, group_id)
SELECT e.entity_id, d.dept_id,
       6429, 6430, NULL,
       '[6429]', '[6430]', NULL,
       1, 5, 1,
       20, '{"low": 20, "high": 20, "medium": 20, "critical": 20}',
       70, '{"low": 75, "high": 25, "medium": 50, "critical": 10}',
       0
FROM (SELECT DISTINCT entity_id FROM GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS
      WHERE entity_id BETWEEN 6313 AND 6336) e
CROSS JOIN (SELECT 1 AS dept_id UNION ALL SELECT 34
            UNION ALL SELECT 43 UNION ALL SELECT 44) d
WHERE NOT EXISTS (
    SELECT 1 FROM GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS x
    WHERE x.entity_id = e.entity_id AND x.department_id = d.dept_id);


-- STEP 4 - verify. Each of the 4 departments must show 24.
SELECT department_id, COUNT(*) AS rows_now
FROM GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS
WHERE entity_id BETWEEN 6313 AND 6336
  AND department_id IN (1, 34, 43, 44)
GROUP BY department_id
ORDER BY department_id;


-- ============================================================================
-- OPTIONAL - switch off the 6 dead departments Zone no longer routes to.
-- Only run this if you want them disabled rather than left dormant.
-- ============================================================================
-- UPDATE GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS
--    SET is_active = 0
--  WHERE entity_id BETWEEN 6313 AND 6336
--    AND department_id IN (5, 16, 28, 29, 31, 33);


-- ============================================================================
-- UNDO - uses the id noted in STEP 2
-- ============================================================================
-- DELETE FROM GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS
--  WHERE id > <max_id_before>
--    AND entity_id BETWEEN 6313 AND 6336
--    AND department_id IN (1, 34, 43, 44);
