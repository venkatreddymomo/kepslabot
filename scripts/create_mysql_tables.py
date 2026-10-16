"""
Script: create all bot tables in MySQL.

Run this BEFORE scripts/copy_data_pg_to_mysql.py (or before flipping
DB_BACKEND=mysql in .env, if you want explicit control over DDL).

Mirrors scripts/create_pg_tables.py — same 21 tables, same indexes, same
constraints — but with MySQL types. Idempotent: CREATE TABLE IF NOT EXISTS,
CREATE INDEX wrapped in try/except.

Reads connection info from .env (MYSQL_HOST, MYSQL_PORT, MYSQL_USER,
MYSQL_PASSWORD, MYSQL_DATABASE).

Usage:
    python scripts/create_mysql_tables.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        print(f"ERROR: .env file not found at {env_path}")
        sys.exit(1)
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

try:
    import pymysql
except ImportError:
    print("ERROR: pymysql not installed. Run: pip install pymysql")
    sys.exit(1)

MY_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
MY_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MY_USER = os.environ.get("MYSQL_USER", "root")
MY_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MY_DATABASE = os.environ.get("MYSQL_DATABASE", "")

if not MY_DATABASE:
    print("ERROR: MYSQL_DATABASE is required in .env")
    sys.exit(1)


# All DDL uses InnoDB + utf8mb4 so JSON works and FKs are honored.
TABLE_OPTIONS = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"


TABLES = [
    # -- 1. hotels ------------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_hotels (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        code        VARCHAR(50)  NOT NULL,
        name        VARCHAR(255) NOT NULL,
        city        VARCHAR(100) NOT NULL,
        timezone    VARCHAR(50)  DEFAULT 'Asia/Kolkata',
        is_active   TINYINT(1)   NOT NULL DEFAULT 1,
        created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_hotels_code UNIQUE (code)
    ) {TABLE_OPTIONS}
    """,

    # -- 2. restaurants -------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_restaurants (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id        INT          NOT NULL,
        code            VARCHAR(50)  NOT NULL,
        name            VARCHAR(255) NOT NULL,
        cuisine         VARCHAR(100),
        opens_at        TIME,
        closes_at       TIME,
        delivers_to_room TINYINT(1)  NOT NULL DEFAULT 0,
        is_active       TINYINT(1)   NOT NULL DEFAULT 1,
        created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_rest UNIQUE (hotel_id, code),
        CONSTRAINT fk_rest_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 3. menu_items --------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_menu_items (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        restaurant_id   INT           NOT NULL,
        name            VARCHAR(255)  NOT NULL,
        description     TEXT,
        price           DECIMAL(10,2) NOT NULL DEFAULT 0.00,
        currency        VARCHAR(3)    NOT NULL DEFAULT 'INR',
        category        VARCHAR(100),
        is_vegetarian   TINYINT(1)    NOT NULL DEFAULT 0,
        is_available    TINYINT(1)    NOT NULL DEFAULT 1,
        created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT fk_menu_rest FOREIGN KEY (restaurant_id) REFERENCES new_bot_restaurants(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 4. guests ------------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_guests (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id        INT          NOT NULL,
        phone_number    VARCHAR(30)  NOT NULL,
        name            VARCHAR(255),
        room_number     VARCHAR(20),
        check_in_date   DATE,
        check_out_date  DATE,
        created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_guest UNIQUE (hotel_id, phone_number),
        CONSTRAINT fk_guest_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 5. bookings ----------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_bookings (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id            INT          NOT NULL,
        guest_id            INT          NOT NULL,
        confirmation_code   VARCHAR(30)  NOT NULL,
        property_name       VARCHAR(255),
        room_number         VARCHAR(20),
        room_type           VARCHAR(100),
        check_in_date       DATE         NOT NULL,
        check_out_date      DATE         NOT NULL,
        num_guests          INT          DEFAULT 1,
        status              VARCHAR(20)  DEFAULT 'reserved',
        source_channel      VARCHAR(20),
        special_requests    TEXT,
        created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_booking_code UNIQUE (confirmation_code),
        CONSTRAINT fk_booking_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE,
        CONSTRAINT fk_booking_guest FOREIGN KEY (guest_id) REFERENCES new_bot_guests(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 6. orders ------------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_orders (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        guest_id            INT           NOT NULL,
        restaurant_id       INT           NOT NULL,
        status              VARCHAR(50)   NOT NULL DEFAULT 'pending',
        total_amount        DECIMAL(10,2) NOT NULL DEFAULT 0.00,
        delivery_location   VARCHAR(100),
        created_at          DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT fk_order_guest FOREIGN KEY (guest_id) REFERENCES new_bot_guests(id),
        CONSTRAINT fk_order_rest  FOREIGN KEY (restaurant_id) REFERENCES new_bot_restaurants(id)
    ) {TABLE_OPTIONS}
    """,

    # -- 7. order_items -------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_order_items (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        order_id        INT           NOT NULL,
        menu_item_id    INT           NOT NULL,
        quantity        INT           NOT NULL DEFAULT 1,
        unit_price      DECIMAL(10,2) NOT NULL DEFAULT 0.00,
        CONSTRAINT fk_oi_order FOREIGN KEY (order_id) REFERENCES new_bot_orders(id) ON DELETE CASCADE,
        CONSTRAINT fk_oi_menu  FOREIGN KEY (menu_item_id) REFERENCES new_bot_menu_items(id)
    ) {TABLE_OPTIONS}
    """,

    # -- 8. conversations -----------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_conversations (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        session_id      VARCHAR(100) NOT NULL,
        hotel_id        INT          NOT NULL,
        guest_id        INT,
        booking_id      INT,
        state           VARCHAR(50)  NOT NULL DEFAULT 'idle',
        pending_action  VARCHAR(100),
        pending_data    JSON,
        channel         VARCHAR(20)  NOT NULL DEFAULT 'web',
        created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_session UNIQUE (session_id),
        CONSTRAINT fk_conv_hotel   FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE,
        CONSTRAINT fk_conv_guest   FOREIGN KEY (guest_id) REFERENCES new_bot_guests(id) ON DELETE SET NULL,
        CONSTRAINT fk_conv_booking FOREIGN KEY (booking_id) REFERENCES new_bot_bookings(id) ON DELETE SET NULL
    ) {TABLE_OPTIONS}
    """,

    # -- 9. messages (table created, no data copied — runtime fills it) -------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_messages (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        conversation_id     INT          NOT NULL,
        role                VARCHAR(20)  NOT NULL,
        content             LONGTEXT     NOT NULL,
        intent              VARCHAR(100),
        confidence          DOUBLE,
        channel             VARCHAR(20)  NOT NULL DEFAULT 'web',
        created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_msg_conv FOREIGN KEY (conversation_id) REFERENCES new_bot_conversations(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 10. business_config --------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_business_config (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id        INT          NOT NULL,
        config_key      VARCHAR(100) NOT NULL,
        config_value    LONGTEXT,
        created_at      DATETIME     NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_config UNIQUE (hotel_id, config_key),
        CONSTRAINT fk_bc_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 11. capabilities -----------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_capabilities (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id        INT          NOT NULL,
        capability_id   VARCHAR(100) NOT NULL,
        description     VARCHAR(255),
        enabled         TINYINT(1)   NOT NULL DEFAULT 1,
        hours           VARCHAR(100),
        created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_cap UNIQUE (hotel_id, capability_id),
        CONSTRAINT fk_cap_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 12. intents ----------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_intents (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id    INT          NOT NULL,
        intent_id   VARCHAR(100) NOT NULL,
        label       VARCHAR(100) NOT NULL,
        enabled     TINYINT(1)   NOT NULL DEFAULT 1,
        created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_intent UNIQUE (hotel_id, intent_id),
        CONSTRAINT fk_intent_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 13. services ---------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_services (
        id                              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id                        INT          NOT NULL,
        service_id                      VARCHAR(100) NOT NULL,
        name                            VARCHAR(255) NOT NULL,
        type                            VARCHAR(50)  NOT NULL DEFAULT 'service',
        description                     TEXT,
        phase_id                        VARCHAR(50),
        is_active                       TINYINT(1)   NOT NULL DEFAULT 1,
        is_builtin                      TINYINT(1)   NOT NULL DEFAULT 0,
        ticketing_enabled               TINYINT(1)   NOT NULL DEFAULT 1,
        ticketing_mode                  VARCHAR(20),
        ticketing_policy                LONGTEXT,
        form_config                     JSON,
        service_prompt_pack             JSON,
        generated_system_prompt         LONGTEXT,
        generated_system_prompt_override TINYINT(1)  NOT NULL DEFAULT 0,
        created_at                      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at                      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_bot_service UNIQUE (hotel_id, service_id),
        CONSTRAINT fk_svc_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 14. prompt_registry --------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_prompt_registry (
        id                      INT AUTO_INCREMENT PRIMARY KEY,
        prompt_key              VARCHAR(128) NOT NULL,
        industry                VARCHAR(32),
        hotel_id                INT,
        content                 LONGTEXT     NOT NULL,
        variables               JSON,
        version                 INT          NOT NULL DEFAULT 1,
        description             TEXT,
        updated_by              VARCHAR(64),
        seeded_from_file_hash   VARCHAR(64),
        is_active               TINYINT(1)   NOT NULL DEFAULT 1,
        created_at              DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at              DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_prompt_scope UNIQUE (prompt_key, industry, hotel_id),
        CONSTRAINT fk_prompt_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 15. kb_files ---------------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_kb_files (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id        INT          NOT NULL,
        original_name   VARCHAR(255) NOT NULL,
        stored_name     VARCHAR(255) NOT NULL,
        content         LONGTEXT     NOT NULL,
        content_hash    VARCHAR(64),
        created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_kb_file UNIQUE (hotel_id, stored_name),
        CONSTRAINT fk_kb_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 16. bot_hotel_images_scraper -----------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS bot_hotel_images_scraper (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id        INT          NOT NULL,
        title           VARCHAR(255) NOT NULL,
        description     TEXT,
        image_url       TEXT         NOT NULL,
        category        VARCHAR(120),
        tags            JSON,
        source_label    VARCHAR(255),
        is_active       TINYINT(1)   NOT NULL DEFAULT 1,
        priority        INT          NOT NULL DEFAULT 0,
        created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT fk_img_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 17. new_bot_properties ----------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_properties (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id        INT          NOT NULL,
        property_slug   VARCHAR(100) NOT NULL,
        name            VARCHAR(255) NOT NULL,
        description     TEXT,
        aliases         JSON,
        location        JSON,
        source_names    JSON,
        is_active       TINYINT(1)   NOT NULL DEFAULT 1,
        is_default      TINYINT(1)   NOT NULL DEFAULT 0,
        created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_property_slug UNIQUE (hotel_id, property_slug),
        CONSTRAINT fk_prop_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 18. new_bot_kb_books (page indexing) --------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_kb_books (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id        INT          NOT NULL,
        property_id     INT,
        book_slug       VARCHAR(200) NOT NULL,
        title           VARCHAR(500) NOT NULL,
        description     TEXT,
        aliases         JSON,
        keywords        JSON,
        page_count      INT          NOT NULL DEFAULT 0,
        char_count      INT          NOT NULL DEFAULT 0,
        source_names    JSON,
        content_hash    VARCHAR(64),
        is_active       TINYINT(1)   NOT NULL DEFAULT 1,
        created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_kb_book UNIQUE (hotel_id, property_id, book_slug),
        CONSTRAINT fk_book_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE,
        CONSTRAINT fk_book_prop  FOREIGN KEY (property_id) REFERENCES new_bot_properties(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 19. new_bot_kb_pages (page indexing) --------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_kb_pages (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        hotel_id        INT          NOT NULL,
        property_id     INT,
        document_id     VARCHAR(100),
        kb_file_id      INT,
        page_slug       VARCHAR(100) NOT NULL,
        title           VARCHAR(500),
        content         LONGTEXT     NOT NULL,
        source_name     VARCHAR(500),
        source_path     TEXT,
        location        VARCHAR(500),
        topics          JSON,
        char_count      INT          NOT NULL DEFAULT 0,
        created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT uq_kb_page_slug UNIQUE (hotel_id, page_slug),
        CONSTRAINT fk_page_hotel FOREIGN KEY (hotel_id) REFERENCES new_bot_hotels(id) ON DELETE CASCADE,
        CONSTRAINT fk_page_prop  FOREIGN KEY (property_id) REFERENCES new_bot_properties(id) ON DELETE CASCADE,
        CONSTRAINT fk_page_file  FOREIGN KEY (kb_file_id) REFERENCES new_bot_kb_files(id) ON DELETE SET NULL
    ) {TABLE_OPTIONS}
    """,

    # -- 20. new_bot_kb_book_pages (book ↔ page link) ------------------------
    f"""
    CREATE TABLE IF NOT EXISTS new_bot_kb_book_pages (
        book_id         INT NOT NULL,
        page_id         INT NOT NULL,
        ordinal         INT NOT NULL DEFAULT 0,
        PRIMARY KEY (book_id, page_id),
        CONSTRAINT fk_bp_book FOREIGN KEY (book_id) REFERENCES new_bot_kb_books(id) ON DELETE CASCADE,
        CONSTRAINT fk_bp_page FOREIGN KEY (page_id) REFERENCES new_bot_kb_pages(id) ON DELETE CASCADE
    ) {TABLE_OPTIONS}
    """,

    # -- 21. scrape_jobs (KB scraper sub-app — no FK to bot tables) ----------
    f"""
    CREATE TABLE IF NOT EXISTS scrape_jobs (
        id                  VARCHAR(36)  PRIMARY KEY,
        url                 TEXT         NOT NULL,
        status              VARCHAR(30)  DEFAULT 'pending',
        progress_pct        INT          DEFAULT 0,
        progress_msg        VARCHAR(500) DEFAULT 'Queued...',
        pages_found         INT          DEFAULT 0,
        pages_crawled       INT          DEFAULT 0,
        pages_failed        INT          DEFAULT 0,
        properties_found    INT          DEFAULT 0,
        properties_data     LONGTEXT,
        review_data         LONGTEXT,
        job_context_data    LONGTEXT,
        queue_state         VARCHAR(20)  DEFAULT 'idle',
        task_type           VARCHAR(50)  DEFAULT '',
        task_payload        LONGTEXT,
        queue_attempts      INT          DEFAULT 0,
        max_attempts        INT          DEFAULT 1,
        next_retry_at       DATETIME,
        worker_id           VARCHAR(100) DEFAULT '',
        worker_started_at   DATETIME,
        worker_heartbeat_at DATETIME,
        output_dir          TEXT,
        kb_preview          LONGTEXT,
        error_message       LONGTEXT,
        created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at        DATETIME
    ) {TABLE_OPTIONS}
    """,
]


# Index DDL — wrapped in IF NOT EXISTS pattern via try/except since
# MySQL < 8.0.13 doesn't support CREATE INDEX IF NOT EXISTS.
INDEXES = [
    ("idx_restaurants_hotel_id",   "new_bot_restaurants",      "(hotel_id)"),
    ("idx_menu_items_rest_id",     "new_bot_menu_items",       "(restaurant_id)"),
    ("idx_guests_hotel_id",        "new_bot_guests",           "(hotel_id)"),
    ("idx_bookings_hotel_id",      "new_bot_bookings",         "(hotel_id)"),
    ("idx_bookings_guest_id",      "new_bot_bookings",         "(guest_id)"),
    ("idx_conversations_hotel_id", "new_bot_conversations",    "(hotel_id)"),
    ("idx_messages_conv_id",       "new_bot_messages",         "(conversation_id)"),
    ("idx_business_config_hotel",  "new_bot_business_config",  "(hotel_id)"),
    ("idx_capabilities_hotel",     "new_bot_capabilities",     "(hotel_id)"),
    ("idx_intents_hotel",          "new_bot_intents",          "(hotel_id)"),
    ("idx_services_hotel",         "new_bot_services",         "(hotel_id)"),
    ("idx_prompt_registry_key",    "new_bot_prompt_registry",  "(prompt_key)"),
    ("idx_kb_files_hotel",         "new_bot_kb_files",         "(hotel_id)"),
    ("idx_images_hotel",           "bot_hotel_images_scraper", "(hotel_id)"),
    # -- new KB / property tables (page indexing) -------------------------
    ("idx_properties_hotel_id",     "new_bot_properties",      "(hotel_id)"),
    ("idx_properties_hotel_active", "new_bot_properties",      "(hotel_id, is_active)"),
    ("idx_kb_books_hotel_id",       "new_bot_kb_books",        "(hotel_id)"),
    ("idx_kb_books_hotel_property", "new_bot_kb_books",        "(hotel_id, property_id)"),
    ("idx_kb_books_hotel_active",   "new_bot_kb_books",        "(hotel_id, is_active)"),
    ("idx_kb_pages_hotel_id",       "new_bot_kb_pages",        "(hotel_id)"),
    ("idx_kb_pages_hotel_property", "new_bot_kb_pages",        "(hotel_id, property_id)"),
    ("idx_kb_book_pages_book",      "new_bot_kb_book_pages",   "(book_id)"),
    ("idx_kb_book_pages_page",      "new_bot_kb_book_pages",   "(page_id)"),
]


TABLE_NAMES = [
    "new_bot_hotels", "new_bot_restaurants", "new_bot_menu_items",
    "new_bot_guests", "new_bot_bookings", "new_bot_orders",
    "new_bot_order_items", "new_bot_conversations", "new_bot_messages",
    "new_bot_business_config", "new_bot_capabilities", "new_bot_intents",
    "new_bot_services", "new_bot_prompt_registry", "new_bot_kb_files",
    "bot_hotel_images_scraper", "new_bot_properties", "new_bot_kb_books",
    "new_bot_kb_pages", "new_bot_kb_book_pages", "scrape_jobs",
]


def _index_exists(cur, table: str, index: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND index_name = %s
        LIMIT 1
        """,
        (table, index),
    )
    return cur.fetchone() is not None


def main() -> int:
    print(f"MySQL: {MY_HOST}:{MY_PORT}/{MY_DATABASE}")

    try:
        conn = pymysql.connect(
            host=MY_HOST, port=MY_PORT,
            user=MY_USER, password=MY_PASSWORD,
            database=MY_DATABASE,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
        )
    except Exception as e:
        print(f"ERROR: cannot connect to MySQL: {e}")
        return 1

    try:
        with conn.cursor() as cur:
            print("\n-- Creating tables --")
            for ddl in TABLES:
                first_line = " ".join(ddl.split())[:80]
                try:
                    cur.execute(ddl)
                    print(f"  OK   {first_line}")
                except Exception as e:
                    print(f"  FAIL {first_line}\n       {e}")

            print("\n-- Creating indexes --")
            for index_name, table_name, columns in INDEXES:
                try:
                    if _index_exists(cur, table_name, index_name):
                        print(f"  SKIP {index_name} (already exists)")
                        continue
                    cur.execute(f"CREATE INDEX {index_name} ON {table_name} {columns}")
                    print(f"  OK   {index_name} ON {table_name}")
                except Exception as e:
                    print(f"  FAIL {index_name}: {e}")

            print("\n-- Verifying tables present --")
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name IN %s
                """,
                (tuple(TABLE_NAMES),),
            )
            present = {row[0] for row in cur.fetchall()}
            missing = [t for t in TABLE_NAMES if t not in present]
            if missing:
                print(f"  MISSING: {missing}")
            else:
                print(f"  All {len(TABLE_NAMES)} tables present.")
    finally:
        conn.close()

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
