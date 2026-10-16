-- Migration: add new_bot_messages.display_payload (LONGTEXT, nullable)
-- Stores the rich display each turn rendered so the admin chat-history page can
-- replay exactly what the guest saw: beautified text, image cards, form schema,
-- and submitted form values.
--
-- Backward-compatible: NULL on every existing row → the UI falls back to the
-- plain `content` text (current behavior). No backfill required.
--
-- Run once against the production MySQL (GHN_PROD_BAK / new_bot_* tables):
--   mysql -h 172.16.5.32 -u <user> -p <database> < tools/migration_add_display_payload.sql
--
-- MySQL has no portable "ADD COLUMN IF NOT EXISTS", so this checks
-- information_schema first and is safe to run more than once.

SET @ddl := (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE new_bot_messages ADD COLUMN display_payload LONGTEXT NULL AFTER channel',
    'SELECT "display_payload already exists — nothing to do"'
  )
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME   = 'new_bot_messages'
    AND COLUMN_NAME  = 'display_payload'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
