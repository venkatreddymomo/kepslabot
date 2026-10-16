"""
Async database layer for KePSLA (MySQL).

Defines:
- Async engine + session factory
- Base model class
- ORM models that match manual tables (prefix new_bot_)

NOTE: All tables use INT AUTO_INCREMENT for IDs (not UUID).
This makes debugging easier and matches recreate_tables.py structure.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.mysql import DECIMAL, LONGTEXT
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, relationship


# JSON column that uses native JSONB on Postgres (with GIN-index support)
# and falls back to JSON on MySQL/SQLite. Use this for any JSON column that
# needs to be queried with path/containment operators on Postgres.
JSONType = JSON().with_variant(JSONB(), "postgresql")

# TEXT that maps to MySQL LONGTEXT (4 GB) instead of TEXT (64 KB) so full
# untrimmed raw LLM I/O and backend dumps fit. Plain TEXT elsewhere.
LongText = Text().with_variant(LONGTEXT(), "mysql")

# BIGINT auto-increment PK on MySQL, but INTEGER on SQLite — SQLite only
# auto-increments an INTEGER PRIMARY KEY (rowid alias), not BIGINT. Keeps the
# log tables working on the local/dev SQLite fallback as well as prod MySQL.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")

from config.settings import settings

# Fallback should stay non-MySQL when DATABASE_URL is empty.
DEFAULT_FALLBACK_DATABASE_URL = (
    str(getattr(settings, "pg_database_url", "") or "").strip()
    or "sqlite+aiosqlite:///./kepsla_bot.db"
)
ACTIVE_DATABASE_URL = str(getattr(settings, "database_url", "") or "").strip() or DEFAULT_FALLBACK_DATABASE_URL


def _build_connect_args(database_url: str) -> dict[str, Any]:
    """
    Build driver-safe connect args.

    - MySQL + aiomysql: supports `connect_timeout`
    - PostgreSQL + asyncpg: supports `timeout`
    - SQLite + aiosqlite: should not receive `connect_timeout`
    """
    try:
        parsed = make_url(database_url)
        backend = str(parsed.get_backend_name() or "").lower()
        driver = str(parsed.get_driver_name() or "").lower()
    except Exception:
        return {}

    if backend == "mysql" and "aiomysql" in driver:
        return {"connect_timeout": 30}
    if backend in {"postgresql", "postgres"} and "asyncpg" in driver:
        return {"timeout": 30, "server_settings": {"search_path": "guest_chatbot"}}
    if backend == "sqlite":
        # timeout=30 makes SQLite wait up to 30s for a write lock instead of
        # immediately raising "database is locked" under concurrent load.
        return {"timeout": 30}
    return {}


Base = declarative_base()


class TimestampMixin:
    """created_at / updated_at columns with server defaults.

    timezone=True so SQLAlchemy emits TIMESTAMP WITH TIME ZONE on Postgres,
    matching the TIMESTAMPTZ columns created by scripts/create_pg_tables.py
    and accepting tz-aware Python datetimes from the app layer.
    """

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )


class Hotel(Base, TimestampMixin):
    __tablename__ = "new_bot_hotels"
    __table_args__ = (
        UniqueConstraint("code", name="uq_hotels_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)
    city = Column(String(100), nullable=False)
    timezone = Column(String(50), server_default=text("'Asia/Kolkata'"))
    is_active = Column(Boolean, nullable=False, server_default=text("1"))

    restaurants = relationship(
        "Restaurant",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    guests = relationship(
        "Guest",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    conversations = relationship(
        "Conversation",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    # Config relationships
    business_configs = relationship(
        "BusinessConfig",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    capabilities = relationship(
        "Capability",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    intents = relationship(
        "Intent",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    bot_services = relationship(
        "BotService",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    kb_files = relationship(
        "KBFile",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    image_assets = relationship(
        "ImageAsset",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    bookings = relationship(
        "Booking",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )


class Restaurant(Base, TimestampMixin):
    __tablename__ = "new_bot_restaurants"
    __table_args__ = (
        UniqueConstraint("hotel_id", "code", name="uq_rest"),
        Index("idx_restaurants_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    code = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)
    cuisine = Column(String(100), nullable=True)
    opens_at = Column(Time, nullable=True)
    closes_at = Column(Time, nullable=True)
    delivers_to_room = Column(Boolean, nullable=False, server_default=text("0"))
    is_active = Column(Boolean, nullable=False, server_default=text("1"))

    hotel = relationship("Hotel", back_populates="restaurants")
    menu_items = relationship(
        "MenuItem",
        back_populates="restaurant",
        cascade="all, delete-orphan",
    )
    orders = relationship("Order", back_populates="restaurant")


class MenuItem(Base, TimestampMixin):
    __tablename__ = "new_bot_menu_items"
    __table_args__ = (
        Index("idx_menu_items_restaurant_id", "restaurant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    restaurant_id = Column(
        Integer,
        ForeignKey("new_bot_restaurants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(DECIMAL(10, 2, asdecimal=True), nullable=False, server_default=text("0.00"))
    currency = Column(String(3), nullable=False, server_default=text("'INR'"))
    category = Column(String(100), nullable=True)
    is_vegetarian = Column(Boolean, nullable=False, server_default=text("0"))
    is_available = Column(Boolean, nullable=False, server_default=text("1"))

    restaurant = relationship("Restaurant", back_populates="menu_items")
    order_items = relationship("OrderItem", back_populates="menu_item")


class Guest(Base, TimestampMixin):
    __tablename__ = "new_bot_guests"
    __table_args__ = (
        UniqueConstraint("hotel_id", "phone_number", name="uq_guest"),
        Index("idx_guests_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    phone_number = Column(String(30), nullable=False)
    name = Column(String(255), nullable=True)
    room_number = Column(String(20), nullable=True)
    room_type = Column(String(100), nullable=True)
    property_name = Column(String(255), nullable=True)
    property_scope_id = Column(String(100), nullable=True)
    check_in_date = Column(Date, nullable=True)
    check_out_date = Column(Date, nullable=True)

    hotel = relationship("Hotel", back_populates="guests")
    bookings = relationship(
        "Booking",
        back_populates="guest",
        cascade="all, delete-orphan",
    )
    orders = relationship(
        "Order",
        back_populates="guest",
        cascade="all, delete-orphan",
    )
    conversations = relationship("Conversation", back_populates="guest")


class Booking(Base, TimestampMixin):
    """Stores individual guest bookings/stays. Separate from Guest to support
    repeat visits and multi-property stays."""
    __tablename__ = "new_bot_bookings"
    __table_args__ = (
        UniqueConstraint("confirmation_code", name="uq_booking_code"),
        Index("idx_bookings_hotel_id", "hotel_id"),
        Index("idx_bookings_guest_id", "guest_id"),
        Index("idx_bookings_hotel_status", "hotel_id", "status"),
        Index("idx_bookings_dates", "hotel_id", "check_in_date", "check_out_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    guest_id = Column(
        Integer,
        ForeignKey("new_bot_guests.id", ondelete="CASCADE"),
        nullable=False,
    )
    confirmation_code = Column(String(30), nullable=False)
    property_name = Column(String(255), nullable=True)
    property_scope_id = Column(String(100), nullable=True)
    room_number = Column(String(20), nullable=True)
    room_type = Column(String(100), nullable=True)
    check_in_date = Column(Date, nullable=False)
    check_out_date = Column(Date, nullable=False)
    num_guests = Column(Integer, nullable=True, server_default=text("1"))
    status = Column(String(20), nullable=False, server_default=text("'reserved'"))
    source_channel = Column(String(20), nullable=True)
    special_requests = Column(Text, nullable=True)

    hotel = relationship("Hotel", back_populates="bookings")
    guest = relationship("Guest", back_populates="bookings")
    conversations = relationship("Conversation", back_populates="booking")


class Order(Base, TimestampMixin):
    __tablename__ = "new_bot_orders"
    __table_args__ = (
        Index("idx_orders_guest_id", "guest_id"),
        Index("idx_orders_restaurant_id", "restaurant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    guest_id = Column(
        Integer,
        ForeignKey("new_bot_guests.id"),
        nullable=False,
    )
    restaurant_id = Column(
        Integer,
        ForeignKey("new_bot_restaurants.id"),
        nullable=False,
    )
    status = Column(String(50), nullable=False, server_default=text("'pending'"))
    total_amount = Column(DECIMAL(10, 2, asdecimal=True), nullable=False, server_default=text("0.00"))
    delivery_location = Column(String(100), nullable=True)

    guest = relationship("Guest", back_populates="orders")
    restaurant = relationship("Restaurant", back_populates="orders")
    order_items = relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
    )


class OrderItem(Base):
    __tablename__ = "new_bot_order_items"
    __table_args__ = (
        Index("idx_order_items_order_id", "order_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        Integer,
        ForeignKey("new_bot_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    menu_item_id = Column(
        Integer,
        ForeignKey("new_bot_menu_items.id"),
        nullable=False,
    )
    quantity = Column(Integer, nullable=False, server_default=text("1"))
    unit_price = Column(DECIMAL(10, 2, asdecimal=True), nullable=False, server_default=text("0.00"))

    order = relationship("Order", back_populates="order_items")
    menu_item = relationship("MenuItem", back_populates="order_items")


class Conversation(Base, TimestampMixin):
    __tablename__ = "new_bot_conversations"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_session"),
        Index("idx_conversations_hotel_id", "hotel_id"),
        Index("idx_conversations_guest_id", "guest_id"),
        Index("idx_conversations_hotel_state", "hotel_id", "state"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    guest_id = Column(
        Integer,
        ForeignKey("new_bot_guests.id", ondelete="SET NULL"),
        nullable=True,
    )
    booking_id = Column(
        Integer,
        ForeignKey("new_bot_bookings.id", ondelete="SET NULL"),
        nullable=True,
    )
    state = Column(String(50), nullable=False, server_default=text("'idle'"))
    pending_action = Column(String(100), nullable=True)
    pending_data = Column(JSON, nullable=True)
    channel = Column(String(20), nullable=False, server_default=text("'web'"))

    hotel = relationship("Hotel", back_populates="conversations")
    guest = relationship("Guest", back_populates="conversations")
    booking = relationship("Booking", back_populates="conversations")
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class Message(Base):
    __tablename__ = "new_bot_messages"
    __table_args__ = (
        Index("idx_messages_conversation_id", "conversation_id"),
        Index("idx_messages_conv_created", "conversation_id", "created_at", "id"),
        Index("idx_messages_conv_uid", "conversation_uid", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(
        Integer,
        ForeignKey("new_bot_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Rotating per-thread id (the widget's conversation_id). Distinct from the
    # integer conversation_id FK above (which is one row per session). Used to
    # scope chat/LLM history to ONE thread so a new conversation starts clean.
    # NULL for legacy/pre-split rows → simply excluded from any new thread.
    conversation_uid = Column(String(100), nullable=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    intent = Column(String(100), nullable=True)
    confidence = Column(Float, nullable=True)
    channel = Column(String(20), nullable=False, server_default=text("'web'"))
    # JSON blob holding the rich display the guest actually saw for this turn:
    # {display_message, images:[...], form_fields:[...], form_submission:{...}}.
    # NULL for older rows → chat history falls back to `content` (plain text),
    # so this is fully backward-compatible and needs no backfill.
    display_payload = Column(Text(length=4294967295), nullable=True)  # LONGTEXT
    # Inserts always set this from the in-memory Message.timestamp (microsecond
    # precise). On MySQL the physical column is widened to DATETIME(6) by the
    # init_db migration so that sub-second order survives and chat history renders
    # turns in true order; SQLite already stores microseconds.
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    conversation = relationship("Conversation", back_populates="messages")


class Ticket(Base, TimestampMixin):
    """
    new_bot_tickets — stores bot-raised tickets.

    Dual-written alongside the existing local JSON / Lumira API flow so the
    admin UI always has a queryable source of truth even when the external
    ticketing API is down.  FKs are SET NULL so deleting a conversation or
    guest record never cascades a ticket deletion.
    """
    __tablename__ = "new_bot_tickets"
    __table_args__ = (
        Index("idx_tickets_hotel_id",   "hotel_id"),
        Index("idx_tickets_session_id", "session_id"),
        Index("idx_tickets_status",     "status"),
        Index("idx_tickets_created_at", "created_at"),
        Index("idx_tickets_hotel_status", "hotel_id", "status"),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    ticket_ref       = Column(String(100),  nullable=True)   # "LOCAL-1" / Lumira ticket ID
    hotel_id         = Column(Integer, ForeignKey("new_bot_hotels.id",        ondelete="SET NULL"), nullable=True)
    conversation_id  = Column(Integer, ForeignKey("new_bot_conversations.id", ondelete="SET NULL"), nullable=True)
    guest_db_id      = Column(Integer, ForeignKey("new_bot_guests.id",        ondelete="SET NULL"), nullable=True)

    # Identity
    session_id       = Column(String(255),  nullable=True)
    # Rotating per-thread id (the widget's conversation_id) captured when the
    # ticket was raised. Scopes the ticket's Conversation History to the ONE
    # chat thread that produced it, instead of every thread in the session.
    # Matches new_bot_messages.conversation_uid for the same thread.
    conversation_uid = Column(String(100),  nullable=True)
    guest_name       = Column(String(255),  nullable=True)
    guest_id         = Column(String(255),  nullable=True)   # external guest identifier
    room_number      = Column(String(50),   nullable=True)
    hotel_code       = Column(String(50),   nullable=True)
    property_name    = Column(String(255),  nullable=True)
    source           = Column(String(50),   nullable=True)   # web / whatsapp

    # Ticket content
    issue            = Column(Text,         nullable=True)   # concise problem
    message          = Column(Text,         nullable=True)   # full conversation context
    description      = Column(Text,         nullable=True)   # short LLM staff-facing summary (KePSLA UI)
    category         = Column(String(100),  nullable=True,  server_default=text("'complaint'"))
    sub_category     = Column(String(100),  nullable=True)
    phase            = Column(String(50),   nullable=True)   # booking / pre_checkin / during_stay / post_checkout

    # Management
    priority         = Column(String(20),   nullable=True,  server_default=text("'medium'"))
    status           = Column(String(30),   nullable=True,  server_default=text("'open'"))
    department       = Column(String(100),  nullable=True)
    assignee         = Column(String(255),  nullable=True)
    manager_notes    = Column(Text,         nullable=True)
    assignee_notes   = Column(Text,         nullable=True)

    # Escalation / SLA
    is_escalated     = Column(Boolean,      nullable=False, server_default=text("0"))
    escalation_stage = Column(String(50),   nullable=True)
    sla_due_at       = Column(DateTime(timezone=True), nullable=True)
    closed_at        = Column(DateTime(timezone=True), nullable=True)

    # Extra
    sentiment_score       = Column(String(20),  nullable=True)
    compensation_type     = Column(String(50),  nullable=True)
    compensation_amount   = Column(Float,       nullable=True)

    # Full payloads for audit / debugging
    lumira_payload   = Column(JSONType,  nullable=True)
    lumira_response  = Column(JSONType,  nullable=True)


class NewBotSession(Base, TimestampMixin):
    """
    new_bot_sessions — one stable lead/profile row per chat session.

    Captured once on the FIRST contact form a guest submits (works for any
    flow, not just Zone). Stores the durable identity the guest typed —
    name / phone / email / chosen location — plus session + hotel linkage and
    the raw first-form payload for audit.

    This is an ADDITIONAL table. It does not replace new_bot_conversations
    (chat state), new_bot_guests (post-booking/stay data), new_bot_messages
    (turn log), or new_bot_tickets. It is the pre-booking identity source used
    to prefill later tickets/forms and to show a real name in history views.

    FKs are SET NULL so deleting a conversation/hotel never deletes the row.
    """
    __tablename__ = "new_bot_sessions"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_new_bot_session"),
        Index("idx_new_bot_sessions_hotel_id", "hotel_id"),
        Index("idx_new_bot_sessions_phone", "guest_phone"),
        Index("idx_new_bot_sessions_email", "guest_email"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id      = Column(String(100), nullable=False)
    conversation_id = Column(Integer, ForeignKey("new_bot_conversations.id", ondelete="SET NULL"), nullable=True)
    hotel_id        = Column(Integer, ForeignKey("new_bot_hotels.id",        ondelete="SET NULL"), nullable=True)
    hotel_code      = Column(String(50),  nullable=True)

    # Stable identity typed by the guest in the first form.
    guest_name      = Column(String(255), nullable=True)
    guest_phone     = Column(String(30),  nullable=True)
    guest_email     = Column(String(255), nullable=True)
    location        = Column(String(255), nullable=True)   # dropdown location the guest chose

    channel         = Column(String(20),  nullable=True)   # web / whatsapp
    source          = Column(String(50),  nullable=True)   # ticket_source / widget
    raw_form_json   = Column(JSONType,    nullable=True)   # full first-form payload (audit)


class BusinessConfig(Base, TimestampMixin):
    """Stores business configuration in database."""
    __tablename__ = "new_bot_business_config"
    __table_args__ = (
        UniqueConstraint("hotel_id", "config_key", name="uq_config"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    config_key = Column(String(100), nullable=False)  # e.g., 'business.name', 'business.welcome_message'
    config_value = Column(Text(length=4294967295), nullable=True)  # LONGTEXT JSON payload

    hotel = relationship("Hotel", back_populates="business_configs")


class Capability(Base, TimestampMixin):
    """Stores capabilities per hotel in database."""
    __tablename__ = "new_bot_capabilities"
    __table_args__ = (
        UniqueConstraint("hotel_id", "capability_id", name="uq_cap"),
        Index("idx_capabilities_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    capability_id = Column(String(100), nullable=False)  # e.g., 'food_ordering', 'room_service'
    description = Column(String(255), nullable=True)
    enabled = Column(Boolean, nullable=False, server_default=text("1"))
    hours = Column(String(100), nullable=True)  # e.g., '24/7' or '9 AM - 9 PM'

    hotel = relationship("Hotel", back_populates="capabilities")


class Intent(Base, TimestampMixin):
    """Stores intents per hotel in database."""
    __tablename__ = "new_bot_intents"
    __table_args__ = (
        UniqueConstraint("hotel_id", "intent_id", name="uq_intent"),
        Index("idx_intents_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    intent_id = Column(String(100), nullable=False)  # e.g., 'greeting', 'order_food'
    label = Column(String(100), nullable=False)
    enabled = Column(Boolean, nullable=False, server_default=text("1"))

    hotel = relationship("Hotel", back_populates="intents")


class BotService(Base, TimestampMixin):
    """Stores all service configuration per hotel. Primary persistent store."""
    __tablename__ = "new_bot_services"
    __table_args__ = (
        UniqueConstraint("hotel_id", "service_id", name="uq_bot_service"),
        Index("idx_bot_services_hotel_id", "hotel_id"),
        Index("idx_bot_services_phase_id", "hotel_id", "phase_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    service_id = Column(String(100), nullable=False)
    name = Column(String(255), nullable=False)
    type = Column(String(50), nullable=False, server_default=text("'service'"))
    description = Column(Text, nullable=True)
    phase_id = Column(String(50), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("1"))
    is_builtin = Column(Boolean, nullable=False, server_default=text("0"))
    ticketing_enabled = Column(Boolean, nullable=False, server_default=text("1"))
    ticketing_mode = Column(String(20), nullable=True)
    ticketing_policy = Column(Text, nullable=True)
    form_config = Column(JSON, nullable=True)
    service_prompt_pack = Column(JSON, nullable=True)
    generated_system_prompt = Column(Text, nullable=True)
    generated_system_prompt_override = Column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )

    hotel = relationship("Hotel", back_populates="bot_services")


class PromptRegistry(Base, TimestampMixin):
    """
    DB-backed registry for every customizable prompt.

    Scope:
      - industry-default row: industry set, hotel_id NULL  (one per supported industry)
      - hotel override row:   hotel_id set                  (industry may be NULL)

    Resolution order (see PromptRegistryService.get):
      hotel override  ->  industry default  ->  raise PromptMissingError
    """
    __tablename__ = "new_bot_prompt_registry"
    __table_args__ = (
        # model_variant is part of the scope key: NULL = shared default (used by
        # gpt-4.1-mini and any model without its own variant); a value like
        # 'gpt-5.4-mini' scopes a row to that model only. See prompt_registry_service.
        UniqueConstraint(
            "prompt_key", "industry", "hotel_id", "model_variant", name="uq_prompt_scope"
        ),
        Index("idx_prompt_registry_key", "prompt_key"),
        Index("idx_prompt_registry_hotel", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    prompt_key = Column(String(128), nullable=False)
    industry = Column(String(32), nullable=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Per-model prompt variant. NULL = shared default (the historical/4.1 rows).
    # A model id here (e.g. 'gpt-5.4-mini') makes this row apply ONLY when that
    # model is active. Added 2026-07 for the per-model prompt system.
    model_variant = Column(String(64), nullable=True)
    content = Column(Text(length=4294967295), nullable=False)  # LONGTEXT
    variables = Column(JSON, nullable=True)
    version = Column(Integer, nullable=False, server_default=text("1"))
    description = Column(Text, nullable=True)
    updated_by = Column(String(64), nullable=True)
    seeded_from_file_hash = Column(String(64), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("1"))


class KBFile(Base, TimestampMixin):
    """Stores uploaded knowledge base file content in the database for persistence."""
    __tablename__ = "new_bot_kb_files"
    __table_args__ = (
        UniqueConstraint("hotel_id", "stored_name", name="uq_kb_file"),
        Index("idx_kb_files_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    original_name = Column(String(255), nullable=False)
    stored_name = Column(String(255), nullable=False)
    content = Column(Text(length=4294967295), nullable=False)  # LONGTEXT
    content_hash = Column(String(64), nullable=True)

    hotel = relationship("Hotel", back_populates="kb_files")


class Property(Base, TimestampMixin):
    """First-class property (library) per hotel for multi-property hotels.

    In single-property hotels, one row per hotel with is_default=True.
    In multi-property hotels, one row per library/property.

    Books and pages reference property_id (nullable) so the same KB can hold
    property-specific content AND common/shared content (property_id IS NULL).
    """

    __tablename__ = "new_bot_properties"
    __table_args__ = (
        UniqueConstraint("hotel_id", "property_slug", name="uq_property_slug"),
        Index("idx_properties_hotel_id", "hotel_id"),
        Index("idx_properties_hotel_active", "hotel_id", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_slug = Column(String(100), nullable=False)  # stable id, e.g. "khil10"
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    aliases = Column(JSONType, nullable=True)  # list[str] alternate names
    location = Column(JSONType, nullable=True)  # {city, address, country, coordinates}
    source_names = Column(JSONType, nullable=True)  # list[str] raw KB source files
    # Use 1/0 literals so the same DDL works on both MySQL (TINYINT) and Postgres (BOOLEAN).
    is_active = Column(Boolean, nullable=False, server_default=text("1"))
    is_default = Column(Boolean, nullable=False, server_default=text("0"))


class KBBook(Base, TimestampMixin):
    """LLM-generated topic book.

    A book groups related KB pages. The main router sees only (title + description +
    aliases + keywords) and decides which books to open — full page content is loaded
    lazily from new_bot_kb_pages via new_bot_kb_book_pages join.

    property_id is nullable: NULL = common/shared content visible to every property.
    """

    __tablename__ = "new_bot_kb_books"
    __table_args__ = (
        UniqueConstraint("hotel_id", "property_id", "book_slug", name="uq_kb_book"),
        Index("idx_kb_books_hotel_id", "hotel_id"),
        Index("idx_kb_books_hotel_property", "hotel_id", "property_id"),
        Index("idx_kb_books_hotel_active", "hotel_id", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id = Column(
        Integer,
        ForeignKey("new_bot_properties.id", ondelete="CASCADE"),
        nullable=True,
    )
    book_slug = Column(String(200), nullable=False)  # e.g. "spa", "rooms"
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)  # LLM-written ≤25 word summary
    aliases = Column(JSONType, nullable=True)  # list[str]
    keywords = Column(JSONType, nullable=True)  # list[str]
    page_count = Column(Integer, nullable=False, server_default=text("0"))
    char_count = Column(Integer, nullable=False, server_default=text("0"))
    source_names = Column(JSONType, nullable=True)  # list[str] contributing KB sources
    content_hash = Column(String(64), nullable=True)
    # Use 1/0 literal for cross-dialect compatibility (MySQL TINYINT + Postgres BOOLEAN).
    is_active = Column(Boolean, nullable=False, server_default=text("1"))


class KBPage(Base, TimestampMixin):
    """Individual KB page (section of an uploaded KB file).

    Pages can belong to multiple books — the mapping lives in new_bot_kb_book_pages.
    property_id scopes the page to a specific property (NULL = common to all).
    """

    __tablename__ = "new_bot_kb_pages"
    __table_args__ = (
        UniqueConstraint("hotel_id", "page_slug", name="uq_kb_page_slug"),
        Index("idx_kb_pages_hotel_id", "hotel_id"),
        Index("idx_kb_pages_hotel_property", "hotel_id", "property_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id = Column(
        Integer,
        ForeignKey("new_bot_properties.id", ondelete="CASCADE"),
        nullable=True,
    )
    document_id = Column(String(100), nullable=True)  # parent KB document id
    kb_file_id = Column(
        Integer,
        ForeignKey("new_bot_kb_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    page_slug = Column(String(100), nullable=False)  # e.g. "page_00042"
    title = Column(String(500), nullable=True)
    # Plain TEXT: unlimited on Postgres; on MySQL we widen to LONGTEXT in init_db()
    # via the _new_columns migration loop if the column exists as narrower TEXT.
    content = Column(Text, nullable=False)
    source_name = Column(String(500), nullable=True)
    source_path = Column(Text, nullable=True)
    location = Column(String(500), nullable=True)
    topics = Column(JSONType, nullable=True)  # list[str] of book_slugs this page belongs to
    char_count = Column(Integer, nullable=False, server_default=text("0"))


class KBBookPage(Base):
    """Many-to-many link between books and pages.

    A page can appear in multiple books (multi-topic page). Ordinal controls the
    order pages are concatenated when the book's context is built.
    """

    __tablename__ = "new_bot_kb_book_pages"
    __table_args__ = (
        Index("idx_kb_book_pages_book", "book_id"),
        Index("idx_kb_book_pages_page", "page_id"),
    )

    book_id = Column(
        Integer,
        ForeignKey("new_bot_kb_books.id", ondelete="CASCADE"),
        primary_key=True,
    )
    page_id = Column(
        Integer,
        ForeignKey("new_bot_kb_pages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal = Column(Integer, nullable=False, server_default=text("0"))


class ImageAsset(Base, TimestampMixin):
    """Stores curated image assets per hotel for LLM-selected media responses.

    property_id is nullable: NULL = shared across all properties under the hotel
    (mirrors the KBBook/KBPage convention). When set, the image is scoped to a
    specific sub-property and only surfaces in chat when that property is the
    active scope.
    """
    __tablename__ = "bot_hotel_images_scraper"
    __table_args__ = (
        Index("idx_image_assets_hotel_id", "hotel_id"),
        Index("idx_image_assets_hotel_active", "hotel_id", "is_active"),
        Index("idx_image_assets_hotel_property", "hotel_id", "property_id"),
        Index(
            "idx_image_assets_hotel_property_active",
            "hotel_id",
            "property_id",
            "is_active",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id = Column(
        Integer,
        ForeignKey("new_bot_properties.id", ondelete="CASCADE"),
        nullable=True,
    )
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    image_url = Column(Text, nullable=False)
    category = Column(String(120), nullable=True)
    tags = Column(JSON, nullable=True)
    source_label = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("1"))
    priority = Column(Integer, nullable=False, server_default=text("0"))

    hotel = relationship("Hotel", back_populates="image_assets")


class KeboEngageSession(Base):
    """
    Lumira-compatible session log table.
    Schema matches GHN_PROD_BAK.KEBO_ENGAGE_SESSIONS exactly so both systems
    can share the same database table.
    """
    __tablename__ = "KEBO_ENGAGE_SESSIONS"
    __table_args__ = {"extend_existing": True}   # safe if table already exists

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), nullable=False, index=True)
    entity_id = Column(Integer, nullable=True)
    group_id = Column(Integer, nullable=True)
    widget_id = Column(Integer, nullable=True, server_default=text("0"))
    conversation_id = Column(String(100), nullable=True)
    sender = Column(String(255), nullable=False)         # USER | BOT | AGENT
    message = Column(Text, nullable=True)
    render = Column(String(100), nullable=True)          # lumira form_key
    media_type = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    message_type = Column(String(30), nullable=True)
    total_cost = Column(DECIMAL, nullable=True, server_default=text("0"))
    total_tokens = Column(Integer, nullable=True, server_default=text("0"))
    session_metadata = Column("metadata", Text, nullable=True)
    from_responder = Column(String(50), nullable=True)
    to_responder = Column(String(50), nullable=True)
    from_agent_id = Column(Integer, nullable=True)
    to_agent_id = Column(Integer, nullable=True)
    is_conversation_active = Column(Boolean, nullable=True, server_default=text("1"))
    use_for_context = Column(Boolean, nullable=False, server_default=text("1"))
    timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )


class WidgetDeployment(Base, TimestampMixin):
    """Embeddable chat widget deployment config (one row per widget_key).

    Replaces the legacy config/widget_deployments.json file so all widget
    config lives in the DB alongside everything else. The bootstrap endpoint
    looks up widget_key (UNIQUE) to fetch theme/size/position for the loader.
    """
    __tablename__ = "new_bot_widget_deployments"
    __table_args__ = (
        UniqueConstraint("widget_key", name="uq_widget_key"),
        Index("idx_widget_hotel_code", "hotel_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    widget_key = Column(String(64), nullable=False)
    hotel_code = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False, server_default=text("'active'"))
    allowed_origins = Column(JSON, nullable=True)
    theme = Column(JSON, nullable=False)
    # MySQL reserves "size" in some contexts; column is named "sizes" to be safe.
    sizes = Column("sizes", JSON, nullable=False)
    position = Column(String(16), nullable=False, server_default=text("'right'"))
    bot_name = Column(String(255), nullable=False)
    phase = Column(String(50), nullable=False, server_default=text("'pre_booking'"))
    auto_open = Column(Boolean, nullable=False, server_default=text("0"))
    logo_url = Column(String(500), nullable=True)
    session_ttl_hours = Column(Integer, nullable=False, server_default=text("24"))
    idle_timeout_minutes = Column(Integer, nullable=False, server_default=text("30"))
    # Pinned chat model for THIS deployment. NULL/'' = unpinned (follow the
    # harness override / env default). A model id here makes every guest turn on
    # this embed run that model + that model's prompt variant, and makes the
    # /chat harness dropdown unable to move it. Resolved server-side only —
    # never read from the iframe URL. Added 2026-07; inert until populated.
    model = Column(String(64), nullable=True)


# ── DB log sink tables ───────────────────────────────────────────────────────
# Purpose-grouped log tables, all fed by services/db_log_sink.py so logs are
# queryable from any environment (local or deployed). These MIRROR the existing
# file loggers (files keep writing); the DB is an additional best-effort target.
# Every row carries the shared identity skeleton so a whole session can be
# reconstructed end-to-end by session_id / trace_id. Pruned at 7 days.

def _log_indexes(prefix: str) -> tuple:
    """Standard index set shared by every new_bot_log_* table."""
    return (
        Index(f"idx_{prefix}_ts", "ts"),
        Index(f"idx_{prefix}_session", "session_id"),
        Index(f"idx_{prefix}_conv_uid", "conversation_uid"),
        Index(f"idx_{prefix}_hotel_ts", "hotel_code", "ts"),
        Index(f"idx_{prefix}_trace", "trace_id"),
        Index(f"idx_{prefix}_level", "level"),
        Index(f"idx_{prefix}_env", "source_env"),
    )


class _LogCommonMixin:
    """Shared identity/skeleton columns for all log tables."""
    id              = Column(BigIntPK, primary_key=True, autoincrement=True)
    ts              = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    source_env      = Column(String(20),  nullable=True)   # local | deployed
    session_id      = Column(String(100), nullable=True)
    conversation_id = Column(Integer,     nullable=True)   # legacy numeric FK slot (unused)
    conversation_uid = Column(String(100), nullable=True)  # rotating per-thread id (frontend conversation_id) — trace logs per conversation
    hotel_code      = Column(String(50),  nullable=True)
    trace_id        = Column(String(100), nullable=True)
    turn_trace_id   = Column(String(100), nullable=True)
    level           = Column(String(16),  nullable=True)   # info | debug | error
    component       = Column(String(120), nullable=True)
    event           = Column(String(120), nullable=True)
    stream          = Column(String(64),  nullable=True)   # originating file logger
    message         = Column(LongText,    nullable=True)   # human-readable line
    payload         = Column(JSONType,    nullable=True)   # full structured record
    created_at      = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class LogMessage(Base, _LogCommonMixin):
    """Per-turn conversation audit: user msg, bot reply, phase, service, intent."""
    __tablename__ = "new_bot_log_messages"
    __table_args__ = _log_indexes("logmsg")

    sender     = Column(String(10),  nullable=True)   # USER | BOT | AGENT
    user_text  = Column(LongText,    nullable=True)
    bot_text   = Column(LongText,    nullable=True)
    phase      = Column(String(50),  nullable=True)
    service_id = Column(String(100), nullable=True)
    intent     = Column(String(120), nullable=True)


class LogChip(Base, _LogCommonMixin):
    """Suggestion-chip lifecycle: chips shown, the one chosen, source, KB-verified."""
    __tablename__ = "new_bot_log_chips"
    __table_args__ = _log_indexes("logchip")

    chips_shown = Column(JSONType,    nullable=True)
    chosen_chip = Column(LongText,    nullable=True)
    chip_source = Column(String(60),  nullable=True)
    kb_verified = Column(Boolean,     nullable=True)
    phase       = Column(String(50),  nullable=True)
    service_id  = Column(String(100), nullable=True)


class LogRouting(Base, _LogCommonMixin):
    """Every routing decision / path the request takes."""
    __tablename__ = "new_bot_log_routing"
    __table_args__ = _log_indexes("logroute")

    route      = Column(String(60),  nullable=True)
    from_topic = Column(String(120), nullable=True)
    to_topic   = Column(String(120), nullable=True)
    decision   = Column(String(60),  nullable=True)
    service_id = Column(String(100), nullable=True)
    phase      = Column(String(50),  nullable=True)
    reason     = Column(LongText,    nullable=True)


class LogBackend(Base, _LogCommonMixin):
    """Full backend path / step trace — the deep 'everything that happened' timeline."""
    __tablename__ = "new_bot_log_backend"
    __table_args__ = _log_indexes("logbe")

    step        = Column(String(120), nullable=True)
    stage       = Column(String(60),  nullable=True)
    endpoint    = Column(String(255), nullable=True)
    method      = Column(String(10),  nullable=True)
    status_code = Column(Integer,     nullable=True)
    duration_ms = Column(Integer,     nullable=True)


class LogLLM(Base, _LogCommonMixin):
    """One row per LLM call: tokens, pricing, latency, and (full) raw I/O."""
    __tablename__ = "new_bot_log_llm"
    __table_args__ = _log_indexes("logllm")

    model           = Column(String(80),  nullable=True)
    call_site       = Column(String(120), nullable=True)   # purpose / where it was called
    input_tokens    = Column(Integer,     nullable=True)
    output_tokens   = Column(Integer,     nullable=True)
    total_tokens    = Column(Integer,     nullable=True)
    input_rate      = Column(Float,       nullable=True)   # USD per 1M input tokens
    output_rate     = Column(Float,       nullable=True)   # USD per 1M output tokens
    input_cost_usd  = Column(Float,       nullable=True)
    output_cost_usd = Column(Float,       nullable=True)
    total_cost_usd  = Column(Float,       nullable=True)
    currency        = Column(String(10),  nullable=True, server_default=text("'USD'"))
    pricing_source  = Column(String(20),  nullable=True)   # default | override
    latency_ms      = Column(Integer,     nullable=True)
    input_text      = Column(LongText,    nullable=True)   # full untrimmed prompt
    output_text     = Column(LongText,    nullable=True)   # full untrimmed completion


class LogTicket(Base, _LogCommonMixin):
    """Ticketing pipeline debug: create attempts, payloads, status transitions."""
    __tablename__ = "new_bot_log_tickets"
    __table_args__ = _log_indexes("logtkt")

    ticket_ref = Column(String(100), nullable=True)
    service_id = Column(String(100), nullable=True)
    status     = Column(String(40),  nullable=True)
    stage      = Column(String(60),  nullable=True)


class LogError(Base, _LogCommonMixin):
    """Every error/exception in one searchable place (fanned out from all streams)."""
    __tablename__ = "new_bot_log_errors"
    __table_args__ = _log_indexes("logerr")

    origin_stream = Column(String(60),  nullable=True)
    error_type    = Column(String(160), nullable=True)
    error_message = Column(LongText,    nullable=True)
    stack         = Column(LongText,    nullable=True)


class LogLLMDaily(Base):
    """Daily LLM cost/token rollup for fast dashboards (refreshed by a job)."""
    __tablename__ = "new_bot_log_llm_daily"
    __table_args__ = (
        UniqueConstraint("day", "source_env", "hotel_code", "model", name="uq_llm_daily"),
        Index("idx_llm_daily_day", "day"),
        Index("idx_llm_daily_hotel", "hotel_code", "day"),
    )

    id            = Column(BigIntPK, primary_key=True, autoincrement=True)
    day           = Column(Date,        nullable=False)
    source_env    = Column(String(20),  nullable=True)
    hotel_code    = Column(String(50),  nullable=True)
    model         = Column(String(80),  nullable=True)
    calls         = Column(Integer,     nullable=False, server_default=text("0"))
    input_tokens  = Column(BigInteger,  nullable=False, server_default=text("0"))
    output_tokens = Column(BigInteger,  nullable=False, server_default=text("0"))
    total_tokens  = Column(BigInteger,  nullable=False, server_default=text("0"))
    total_cost_usd = Column(Float,      nullable=False, server_default=text("0"))
    updated_at    = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class IRDGuestOrders(Base, TimestampMixin):
    """The guest's order book for in-room dining — one row per stay-session.

    THE ONE COPY. Until this table existed the order lived in two places at
    once: the sentence the bot wrote for the guest, and a JSON field it was
    asked to re-type in full every turn. They drifted, and a guest was shown
    "You've added 2 x Chicken Burger" while the kitchen ticket went out with
    coffee and chicken. Both the guest summary and the ticket description are
    now printed from THIS row, so they cannot disagree.

    Ten slots, not a list: a stay is cleared after 24h, so the slots recycle and
    a guest never needs an eleventh in one day. When all ten are used the oldest
    `delivered` slot is reused.

    `cart_N` holds what the guest ordered and anything they asked for with it:

        {"items": [{"item": "Chicken Burger", "qty": 2, "price": 750,
                    "allergens": "gluten, dairy"}],
         "note": "no onions, ring twice - baby sleeping"}

    `status_N` is that order's state, and it is the ONLY thing that says whether
    an order may still be changed:

        ongoing     not sent yet - add, remove and re-price freely
        placed      with the kitchen, still inside the change window
        processing  the kitchen has started it - LOCKED, no edits at all
        delivered   done
        cancelled   withdrawn before it was cooked

    `placed_at_N` is when it went to the kitchen and `ticket_ref_N` is its
    external ticket. The bot is never asked to do clock arithmetic on these -
    code compares them and tells it "you CAN still add to this one" in words.

    Guest identity is COPIED from the GHN guest tables, which are read-only to
    this application. Nothing here is ever written back to GHN.
    """

    __tablename__ = "new_bot_ird_guest_orders"
    __table_args__ = (
        UniqueConstraint("hotel_code", "session_id", name="uq_ird_orders_session"),
        Index("idx_ird_orders_room", "hotel_code", "room_number"),
        Index("idx_ird_orders_updated", "updated_at"),   # the 24h sweep
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_code      = Column(String(50),  nullable=False)
    session_id      = Column(String(100), nullable=False)
    conversation_id = Column(String(100), nullable=True)

    # Copied from GHN. Read-only there, never written back.
    guest_name      = Column(String(255), nullable=True)
    room_number     = Column(String(20),  nullable=True)
    check_in        = Column(Date,        nullable=True)
    check_out       = Column(Date,        nullable=True)

    # Ten order slots. cart / status / when it was placed / its ticket.
    cart_1       = Column(JSONType,    nullable=True)
    status_1     = Column(String(20),  nullable=True)
    placed_at_1  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_1 = Column(String(50),  nullable=True)

    cart_2       = Column(JSONType,    nullable=True)
    status_2     = Column(String(20),  nullable=True)
    placed_at_2  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_2 = Column(String(50),  nullable=True)

    cart_3       = Column(JSONType,    nullable=True)
    status_3     = Column(String(20),  nullable=True)
    placed_at_3  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_3 = Column(String(50),  nullable=True)

    cart_4       = Column(JSONType,    nullable=True)
    status_4     = Column(String(20),  nullable=True)
    placed_at_4  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_4 = Column(String(50),  nullable=True)

    cart_5       = Column(JSONType,    nullable=True)
    status_5     = Column(String(20),  nullable=True)
    placed_at_5  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_5 = Column(String(50),  nullable=True)

    cart_6       = Column(JSONType,    nullable=True)
    status_6     = Column(String(20),  nullable=True)
    placed_at_6  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_6 = Column(String(50),  nullable=True)

    cart_7       = Column(JSONType,    nullable=True)
    status_7     = Column(String(20),  nullable=True)
    placed_at_7  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_7 = Column(String(50),  nullable=True)

    cart_8       = Column(JSONType,    nullable=True)
    status_8     = Column(String(20),  nullable=True)
    placed_at_8  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_8 = Column(String(50),  nullable=True)

    cart_9       = Column(JSONType,    nullable=True)
    status_9     = Column(String(20),  nullable=True)
    placed_at_9  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_9 = Column(String(50),  nullable=True)

    cart_10       = Column(JSONType,    nullable=True)
    status_10     = Column(String(20),  nullable=True)
    placed_at_10  = Column(DateTime(timezone=True), nullable=True)
    ticket_ref_10 = Column(String(50),  nullable=True)



# Async engine + session factory
_engine_connect_args = _build_connect_args(ACTIVE_DATABASE_URL)
_engine_kwargs: dict[str, Any] = {
    "echo": settings.database_echo,
    "pool_pre_ping": True,
}
if _engine_connect_args:
    _engine_kwargs["connect_args"] = _engine_connect_args

engine = create_async_engine(
    ACTIVE_DATABASE_URL,
    **_engine_kwargs,
)

# Enable WAL mode for SQLite so concurrent reads don't block writes.
try:
    _parsed_url = make_url(ACTIVE_DATABASE_URL)
    if str(_parsed_url.get_backend_name() or "").lower() == "sqlite":
        from sqlalchemy import event as _sa_event

        @_sa_event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_wal(dbapi_conn, connection_record):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA busy_timeout=30000")
except Exception:
    pass

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides an async DB session."""
    async with AsyncSessionLocal() as session:
        yield session


async def _column_exists(conn, table: str, column: str) -> bool:
    """Check whether a column exists on the active backend."""
    backend = str(getattr(conn.dialect, "name", "") or "").lower()

    if backend == "mysql":
        result = await conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                  AND column_name = :column_name
                LIMIT 1
                """
            ),
            {"table_name": table, "column_name": column},
        )
        return result.scalar_one_or_none() is not None

    if backend in {"postgresql", "postgres"}:
        result = await conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = :table_name
                  AND column_name = :column_name
                LIMIT 1
                """
            ),
            {"table_name": table, "column_name": column},
        )
        return result.scalar_one_or_none() is not None

    if backend == "sqlite":
        # Table/column names come from a static migration list below.
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        for row in result:
            row_name = str(getattr(row, "_mapping", {}).get("name", row[1]) or "")
            if row_name.lower() == column.lower():
                return True
        return False

    try:
        await conn.execute(text(f"SELECT {column} FROM {table} WHERE 1=0"))
        return True
    except Exception:
        return False


async def _mysql_data_type(conn, table: str, column: str) -> str:
    """Return MySQL DATA_TYPE for a given column (empty when unavailable)."""
    result = await conn.execute(
        text(
            """
            SELECT DATA_TYPE
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
              AND column_name = :column_name
            LIMIT 1
            """
        ),
        {"table_name": table, "column_name": column},
    )
    value = result.scalar_one_or_none()
    return str(value or "").strip().lower()


async def _mysql_datetime_precision(conn, table: str, column: str) -> int:
    """Return MySQL DATETIME_PRECISION (fractional-seconds precision) for a column; 0 when unavailable."""
    result = await conn.execute(
        text(
            """
            SELECT DATETIME_PRECISION
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
              AND column_name = :column_name
            LIMIT 1
            """
        ),
        {"table_name": table, "column_name": column},
    )
    value = result.scalar_one_or_none()
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


async def init_db() -> None:
    """Initialize database tables (safe if tables already exist)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Add columns that may be missing on older deployments.
    # This is intentionally best-effort and safe to run repeatedly.
    _new_columns = [
        ("new_bot_hotels", "timezone", "VARCHAR(50) NULL DEFAULT 'Asia/Kolkata'"),
        ("new_bot_hotels", "is_active", "BOOLEAN NOT NULL DEFAULT 1"),
        ("new_bot_services", "type", "VARCHAR(50) NOT NULL DEFAULT 'service'"),
        ("new_bot_services", "description", "TEXT NULL"),
        ("new_bot_services", "phase_id", "VARCHAR(50) NULL"),
        ("new_bot_services", "is_active", "BOOLEAN NOT NULL DEFAULT 1"),
        ("new_bot_services", "is_builtin", "BOOLEAN NOT NULL DEFAULT 0"),
        ("new_bot_services", "ticketing_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
        ("new_bot_services", "ticketing_policy", "TEXT NULL"),
        ("new_bot_services", "service_prompt_pack", "JSON NULL"),
        ("new_bot_services", "generated_system_prompt", "TEXT NULL"),
        ("new_bot_services", "ticketing_mode", "VARCHAR(20) NULL"),
        ("new_bot_services", "form_config", "JSON NULL"),
        ("new_bot_services", "generated_system_prompt_override", "BOOLEAN NOT NULL DEFAULT 0"),
        ("new_bot_kb_files", "content_hash", "VARCHAR(64) NULL"),
        ("new_bot_business_config", "created_at", "DATETIME NULL DEFAULT CURRENT_TIMESTAMP"),
        (
            "new_bot_business_config",
            "updated_at",
            "DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
        ("new_bot_conversations", "booking_id", "INT NULL"),
        # Rotating per-thread id: scopes chat history + LLM context to one
        # conversation thread so a new chat (dustbin / 30-min idle) starts clean.
        ("new_bot_messages", "conversation_uid", "VARCHAR(100) NULL"),
        ("bot_hotel_images_scraper", "description", "TEXT NULL"),
        ("bot_hotel_images_scraper", "property_id", "INT NULL"),
        ("new_bot_guests", "room_type", "VARCHAR(100) NULL"),
        ("new_bot_guests", "property_name", "VARCHAR(255) NULL"),
        ("new_bot_guests", "property_scope_id", "VARCHAR(100) NULL"),
        ("new_bot_bookings", "property_scope_id", "VARCHAR(100) NULL"),
        ("new_bot_tickets", "description", "TEXT NULL"),
        ("new_bot_tickets", "conversation_uid", "VARCHAR(100) NULL"),
        # Per-thread conversation id on every log table, so prod logs can be
        # traced/filtered by conversation (the frontend's rotating conversation_id).
        ("new_bot_log_messages", "conversation_uid", "VARCHAR(100) NULL"),
        ("new_bot_log_chips", "conversation_uid", "VARCHAR(100) NULL"),
        ("new_bot_log_routing", "conversation_uid", "VARCHAR(100) NULL"),
        ("new_bot_log_backend", "conversation_uid", "VARCHAR(100) NULL"),
        ("new_bot_log_llm", "conversation_uid", "VARCHAR(100) NULL"),
        ("new_bot_log_tickets", "conversation_uid", "VARCHAR(100) NULL"),
        ("new_bot_log_errors", "conversation_uid", "VARCHAR(100) NULL"),
    ]
    # Each ALTER runs in its OWN transaction so one failure (e.g. a metadata-lock
    # timeout while the log sink is mid-write) can't poison the remaining columns.
    # A single shared transaction previously left several log tables without the
    # newly-added column when an early ALTER failed. Retry a few times so a
    # transient lock from the concurrent log-sink writer can't permanently skip a
    # column (which would then make every INSERT to that table fail with 1054).
    import asyncio as _asyncio_mig
    for table, col, col_type in _new_columns:
        for _attempt in range(4):
            try:
                async with engine.begin() as conn:
                    if await _column_exists(conn, table, col):
                        break
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                break
            except Exception:
                # Transient (lock/race) or unsupported DDL — back off and retry.
                try:
                    await _asyncio_mig.sleep(1.5)
                except Exception:
                    pass

    # MySQL-only longtext widening (skip when already LONGTEXT).
    try:
        parsed = make_url(ACTIVE_DATABASE_URL)
        backend = str(parsed.get_backend_name() or "").lower()
    except Exception:
        backend = ""

    if backend == "mysql":
        try:
            async with engine.begin() as conn:
                if await _mysql_data_type(conn, "new_bot_business_config", "config_value") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_business_config MODIFY COLUMN config_value LONGTEXT NULL")
                    )
        except Exception:
            pass

        try:
            async with engine.begin() as conn:
                if await _mysql_data_type(conn, "new_bot_kb_files", "content") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_kb_files MODIFY COLUMN content LONGTEXT NOT NULL")
                    )
                if await _mysql_data_type(conn, "new_bot_services", "generated_system_prompt") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_services MODIFY COLUMN generated_system_prompt LONGTEXT NULL")
                    )
                if await _mysql_data_type(conn, "new_bot_services", "ticketing_policy") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_services MODIFY COLUMN ticketing_policy LONGTEXT NULL")
                    )
                if await _mysql_data_type(conn, "new_bot_prompt_registry", "content") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_prompt_registry MODIFY COLUMN content LONGTEXT NOT NULL")
                    )
        except Exception:
            pass

        # Microsecond precision on message timestamps. The app already records a
        # microsecond-precise timestamp on every Message, but a plain DATETIME
        # column truncates the sub-second part — so when a turn's user and bot
        # messages land in the same second, chat history can't tell which came
        # first and the bot reply can sort above the question. DATETIME(6)
        # preserves the existing precision so ordering is correct going forward.
        try:
            async with engine.begin() as conn:
                if await _mysql_datetime_precision(conn, "new_bot_messages", "created_at") < 6:
                    await conn.execute(
                        text(
                            "ALTER TABLE new_bot_messages "
                            "MODIFY COLUMN created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)"
                        )
                    )
        except Exception:
            pass

    # Postgres-only: GIN indexes on JSONB columns for fast containment/path queries.
    # Used by property filtering, alias/keyword matching, and topic lookup on the
    # new KB tables.
    if backend in {"postgresql", "postgres"}:
        _gin_indexes = [
            ("idx_kb_books_aliases_gin", "new_bot_kb_books", "aliases"),
            ("idx_kb_books_keywords_gin", "new_bot_kb_books", "keywords"),
            ("idx_kb_pages_topics_gin", "new_bot_kb_pages", "topics"),
            ("idx_properties_aliases_gin", "new_bot_properties", "aliases"),
        ]
        for index_name, table_name, column_name in _gin_indexes:
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            f'CREATE INDEX IF NOT EXISTS {index_name} '
                            f'ON {table_name} USING gin ({column_name})'
                        )
                    )
            except Exception:
                # Best effort: skip if the column doesn't exist yet or JSONB not supported.
                pass

    # Best-effort secondary index on (hotel_id, property_id) for image lookup.
    # Safe to run repeatedly: index creation is wrapped in IF NOT EXISTS where
    # supported, and the whole block is swallowed on failure.
    try:
        async with engine.begin() as conn:
            if backend in {"postgresql", "postgres"}:
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_image_assets_hotel_property "
                        "ON bot_hotel_images_scraper (hotel_id, property_id)"
                    )
                )
            elif backend == "mysql":
                exists = await conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM information_schema.statistics
                        WHERE table_schema = DATABASE()
                          AND table_name = 'bot_hotel_images_scraper'
                          AND index_name = 'idx_image_assets_hotel_property'
                        """
                    )
                )
                if (exists.scalar_one_or_none() or 0) == 0:
                    await conn.execute(
                        text(
                            "CREATE INDEX idx_image_assets_hotel_property "
                            "ON bot_hotel_images_scraper (hotel_id, property_id)"
                        )
                    )
    except Exception:
        pass
