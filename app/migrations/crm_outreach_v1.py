"""Add the CRM foundation without changing existing channel/message data.

The caller supplies the engine and metadata. There is deliberately no import of
app.config, app.database or app.main here, and no work at module import time.
"""
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import MetaData

logger = logging.getLogger(__name__)

# A transaction-scoped PostgreSQL lock serializes concurrent app starts.
MIGRATION_LOCK_ID = 220260501


def migrate_crm_outreach(engine: Engine, metadata: MetaData) -> None:
    """Bootstrap a fresh database or add CRM fields to an existing one.

    Fail loudly: swallowing a failed ALTER would let ORM queries select columns
    which do not exist, including when the new feature is disabled.
    """
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError("CRM migration supports PostgreSQL and SQLite only")

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": MIGRATION_LOCK_ID},
            )

        inspector = inspect(connection)
        if inspector.has_table("portals"):
            columns = {column["name"] for column in inspector.get_columns("portals")}
            if "crm_outreach_enabled" not in columns:
                connection.execute(text(
                    "ALTER TABLE portals ADD COLUMN crm_outreach_enabled "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                ))

        if inspector.has_table("message_delivery_tasks"):
            columns = {column["name"] for column in inspector.get_columns("message_delivery_tasks")}
            if "dedup_key" not in columns:
                connection.execute(text(
                    "ALTER TABLE message_delivery_tasks ADD COLUMN dedup_key VARCHAR"
                ))

        # Fresh schema creation and creation of new tables share the same lock
        # and transaction. Existing tables are never replaced or rebuilt.
        metadata.create_all(bind=connection)
        for index in metadata.tables["message_delivery_tasks"].indexes:
            if index.name == "uq_delivery_task_dedup_key":
                index.create(bind=connection, checkfirst=True)

    logger.info("CRM outreach schema is ready; existing data preserved")
