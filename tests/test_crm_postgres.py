"""Opt-in PostgreSQL checks, isolated in a random schema owned by each test.

Run through run_isolated.py with CRM_TEST_POSTGRES_URL explicitly set to a test
database. No fallback to the application's DATABASE_URL is permitted.
"""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, current_thread
import uuid
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, patch

from app.database import Base
from app.migrations.crm_outreach_v1 import migrate_crm_outreach
from test_crm_foundation import create_legacy_schema
from app.config import settings
from app.models import Channel, CrmMessageProvider, CrmMessageStatusEvent, CrmOutgoingMessage, MessageDeliveryTask, Portal
from app.services.crm_outreach import accept_crm_message, record_edna_status
from app.services.crm_worker import _status_lock
from app.services import crm_worker, delivery_worker
from app.services.crm_providers import stage_portal, stage_provider


@unittest.skipUnless(os.environ.get("CRM_TEST_POSTGRES_URL"), "Explicit isolated test PostgreSQL not configured")
class PostgreSqlTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(os.environ["CRM_TEST_POSTGRES_URL"])
        if self.engine.dialect.name != "postgresql":
            self.engine.dispose()
            self.fail("CRM_TEST_POSTGRES_URL must refer to an isolated PostgreSQL database")
        self.schema = "test_crm_" + uuid.uuid4().hex
        with self.engine.begin() as connection:
            connection.exec_driver_sql('CREATE SCHEMA "' + self.schema + '"')
        self.engine.dispose()

        @event.listens_for(self.engine, "connect")
        def search_path(connection, _record):
            with connection.cursor() as cursor:
                cursor.execute('SET search_path TO "' + self.schema + '"')
            connection.commit()

    def tearDown(self):
        try:
            # Only remove this test's freshly created, validated schema.
            self.assertRegex(self.schema, r"^test_crm_[0-9a-f]{32}$")
            with self.engine.begin() as connection:
                connection.exec_driver_sql('DROP SCHEMA "' + self.schema + '" CASCADE')
        finally:
            self.engine.dispose()

    def test_parallel_bootstraps_are_serialized_and_repeatable(self):
        barrier = Barrier(2)

        def migrate():
            barrier.wait(timeout=10)
            migrate_crm_outreach(self.engine, Base.metadata)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(migrate) for _ in range(2)]
            for future in futures:
                future.result(timeout=30)
        migrate_crm_outreach(self.engine, Base.metadata)
        self.assertEqual(set(inspect(self.engine).get_table_names()), set(Base.metadata.tables))

    def seed_outreach(self):
        migrate_crm_outreach(self.engine, Base.metadata)
        session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        with session() as db:
            portal = Portal(member_id="test-portal", client_endpoint="https://portal.invalid/rest/",
                            access_token="synthetic", refresh_token="synthetic", app_token="synthetic-app-token",
                            crm_outreach_enabled=True)
            db.add(portal)
            db.flush()
            channel = Channel(portal_id=portal.id, name="MAX", sender="synthetic-sender", api_key="synthetic",
                              channel_type="MAX", is_active=True)
            db.add(channel)
            db.flush()
            db.add(CrmMessageProvider(portal_id=portal.id, channel_id=channel.id, provider_code="test-code",
                                      desired_state="present", sync_state="ready", applied_revision=1))
            db.commit()
        return session

    def test_concurrent_duplicate_receipts_create_one_message_and_one_send(self):
        session = self.seed_outreach()
        barrier = Barrier(2)
        data = dict(module_id="crm", type="SMS", code="test-code", message_id="same-id", message_to="+79990000001",
                    message_body="test", auth={"member_id": "test-portal", "application_token": "synthetic-app-token"})
        def receive():
            barrier.wait(timeout=10)
            with session() as db:
                row = accept_crm_message(db, data)
                db.commit()
                return row.id
        with patch.object(settings, "crm_outreach_enabled", True), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(receive) for _ in range(2)]
            ids = [future.result(timeout=15) for future in futures]
        self.assertEqual(ids[0], ids[1])
        with session() as db:
            self.assertEqual(db.query(CrmOutgoingMessage).count(), 1)
            self.assertEqual(db.query(MessageDeliveryTask).filter_by(task_type="send_crm_to_edna").count(), 1)

    def test_concurrent_status_duplicates_are_saved_once(self):
        session = self.seed_outreach()
        with patch.object(settings, "crm_outreach_enabled", True), session() as db:
            message = accept_crm_message(db, dict(module_id="crm", type="SMS", code="test-code", message_id="status-id",
                message_to="+79990000001", message_body="test",
                auth={"member_id": "test-portal", "application_token": "synthetic-app-token"}))
            db.commit()
            channel_id, out_id = message.channel_id, message.edna_out_message_id
        barrier = Barrier(2)
        def record():
            barrier.wait(timeout=10)
            with session() as db:
                record_edna_status(db, db.get(Channel, channel_id), {"requestId": out_id, "status": "READ"})
                db.commit()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(record) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        with session() as db:
            self.assertEqual(db.query(CrmMessageStatusEvent).count(), 1)
            self.assertEqual(db.query(CrmOutgoingMessage).one().desired_revision, 2)

    def test_status_advisory_lock_is_exclusive_and_released(self):
        with _status_lock(self.engine, 1) as first:
            self.assertTrue(first)
            with _status_lock(self.engine, 1) as second:
                self.assertFalse(second)
        with _status_lock(self.engine, 1) as reacquired:
            self.assertTrue(reacquired)

    def test_postgres_worker_send_and_real_status_pipeline(self):
        session = self.seed_outreach()
        with patch.object(settings, "crm_outreach_enabled", True), session() as db:
            message = accept_crm_message(db, dict(module_id="crm", type="SMS", code="test-code", message_id="pipeline-id",
                message_to="+79990000001", message_body="test",
                auth={"member_id": "test-portal", "application_token": "synthetic-app-token"}))
            db.commit()
            message_id, channel_id, out_id = message.id, message.channel_id, message.edna_out_message_id
        with patch.object(settings, "crm_outreach_enabled", True), patch.object(delivery_worker, "SessionLocal", session), \
             patch.object(crm_worker, "send_message_max", AsyncMock(return_value={"outMessageId": out_id})) as send:
            asyncio.run(delivery_worker._process_one_task())
            send.assert_awaited_once()
        with session() as db:
            self.assertEqual(db.get(CrmOutgoingMessage, message_id).send_state, "accepted")
            record_edna_status(db, db.get(Channel, channel_id), {"requestId": out_id, "status": "READ"})
            db.query(MessageDeliveryTask).filter_by(task_type="update_crm_status").update(
                {"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)})
            db.commit()
        # Statuses must still be applied when new sending has been switched off.
        with patch.object(settings, "crm_outreach_enabled", False), patch.object(delivery_worker, "SessionLocal", session), \
             patch.object(crm_worker, "update_message_status", AsyncMock()) as update:
            asyncio.run(delivery_worker._process_one_task())
            asyncio.run(delivery_worker._process_one_task())
            update.assert_awaited_once()
            self.assertEqual(update.call_args.args[-1], "delivered")
        with session() as db:
            row = db.get(CrmOutgoingMessage, message_id)
            self.assertEqual((row.send_state, row.applied_revision, row.desired_revision), ("read", 2, 2))

    def test_parallel_preparation_creates_one_provider_and_one_sync_job(self):
        session = self.seed_outreach()
        with session() as db:
            db.query(CrmMessageProvider).delete()
            db.commit()
        barrier = Barrier(2)
        def prepare():
            barrier.wait(timeout=10)
            with session() as db:
                portal = db.query(Portal).with_for_update().one()
                stage_portal(db, portal, force=True)
                db.commit()
        with patch.object(settings, "crm_outreach_enabled", True), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(prepare) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        with session() as db:
            self.assertEqual(db.query(CrmMessageProvider).count(), 1)
            self.assertEqual(db.query(MessageDeliveryTask).filter_by(task_type="sync_crm_provider").count(), 1)

    def queued_message(self, session):
        with patch.object(settings, "crm_outreach_enabled", True), session() as db:
            row = accept_crm_message(db, dict(module_id="crm", type="SMS", code="test-code", message_id="race-id",
                message_to="+79990000001", message_body="test",
                auth={"member_id": "test-portal", "application_token": "synthetic-app-token"}))
            db.commit()
            return row.id, row.channel_id, row.edna_out_message_id

    def test_disconnect_wins_before_worker_claim_and_prevents_http_send(self):
        session = self.seed_outreach()
        message_id, channel_id, _ = self.queued_message(session)
        prepared, release, claim_attempt = Event(), Event(), Event()
        def disconnect():
            with session() as db:
                portal = db.query(Portal).with_for_update().one()
                channel = db.query(Channel).filter_by(id=channel_id).with_for_update().one()
                channel.is_active = False
                stage_provider(db, portal, channel)
                prepared.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("Test release timed out")
                db.commit()
        def claim_marker(_conn, _cursor, statement, _params, _context, _many):
            if "race-worker" in current_thread().name and statement.startswith("UPDATE message_delivery_tasks"):
                claim_attempt.set()
        event.listen(self.engine, "before_cursor_execute", claim_marker)
        try:
            with patch.object(settings, "crm_outreach_enabled", True), patch.object(delivery_worker, "SessionLocal", session), \
                 patch.object(crm_worker, "send_message_max", AsyncMock()) as send, \
                 ThreadPoolExecutor(max_workers=1) as disconnect_pool, ThreadPoolExecutor(max_workers=1, thread_name_prefix="race-worker") as send_pool:
                disconnect_future = disconnect_pool.submit(disconnect)
                self.assertTrue(prepared.wait(timeout=10))
                send_future = send_pool.submit(asyncio.run, delivery_worker._process_one_task())
                try:
                    self.assertTrue(claim_attempt.wait(timeout=10))
                finally:
                    release.set()
                disconnect_future.result(timeout=15)
                send_future.result(timeout=15)
                send.assert_not_awaited()
        finally:
            release.set()
            event.remove(self.engine, "before_cursor_execute", claim_marker)
        with session() as db:
            self.assertEqual(db.get(CrmOutgoingMessage, message_id).send_state, "failed")

    def test_disconnect_during_started_http_preserves_outcome(self):
        session = self.seed_outreach()
        message_id, channel_id, out_id = self.queued_message(session)
        in_http, release = Event(), Event()
        async def send(**_kwargs):
            in_http.set()
            if not release.wait(timeout=10):
                raise RuntimeError("Test HTTP release timed out")
            return {"outMessageId": out_id}
        with patch.object(settings, "crm_outreach_enabled", True), patch.object(delivery_worker, "SessionLocal", session), \
             patch.object(crm_worker, "send_message_max", side_effect=send) as sent, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, delivery_worker._process_one_task())
            try:
                self.assertTrue(in_http.wait(timeout=10))
                with session() as db:
                    portal = db.query(Portal).with_for_update().one()
                    channel = db.query(Channel).filter_by(id=channel_id).with_for_update().one()
                    channel.is_active = False
                    stage_provider(db, portal, channel)
                    db.commit()
            finally:
                release.set()
            future.result(timeout=15)
            sent.assert_awaited_once()
        with session() as db:
            self.assertEqual(db.get(CrmOutgoingMessage, message_id).send_state, "accepted")

    def test_existing_data_is_preserved_on_upgrade(self):
        create_legacy_schema(self.engine)
        migrate_crm_outreach(self.engine, Base.metadata)
        migrate_crm_outreach(self.engine, Base.metadata)
        with self.engine.connect() as connection:
            row = connection.execute(text("SELECT access_token,crm_outreach_enabled FROM portals WHERE id=1")).one()
            self.assertEqual(tuple(row), ("existing-ciphertext", False))
            self.assertEqual(connection.execute(text("SELECT text FROM messages WHERE id=1")).scalar_one(), "Keep this history")
            row = connection.execute(text("SELECT retry_count,dedup_key FROM message_delivery_tasks WHERE id=1")).one()
            self.assertEqual(tuple(row), (2, None))

    def test_migration_failure_rolls_back_added_columns(self):
        create_legacy_schema(self.engine)

        def fail_before_create(_connection, _cursor, statement, _parameters, _context, _many):
            if "CREATE TABLE crm_message_providers" in statement:
                raise RuntimeError("Synthetic DDL failure")

        event.listen(self.engine, "before_cursor_execute", fail_before_create)
        try:
            with self.assertRaisesRegex(RuntimeError, "Synthetic DDL failure"):
                migrate_crm_outreach(self.engine, Base.metadata)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_before_create)
        inspector = inspect(self.engine)
        self.assertNotIn("crm_outreach_enabled", {column["name"] for column in inspector.get_columns("portals")})
        self.assertNotIn("dedup_key", {column["name"] for column in inspector.get_columns("message_delivery_tasks")})
        self.assertNotIn("crm_message_providers", inspector.get_table_names())
        migrate_crm_outreach(self.engine, Base.metadata)

    def test_duplicate_operation_is_rejected_but_null_legacy_keys_are_allowed(self):
        create_legacy_schema(self.engine)
        migrate_crm_outreach(self.engine, Base.metadata)
        statement = text(
            "INSERT INTO message_delivery_tasks "
            "(channel_id,task_type,direction,payload,status,retry_count,next_attempt_at,created_at,updated_at,dedup_key) "
            "VALUES (1,'send_crm_to_edna','outgoing','{}','pending',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,:key)"
        )
        # The synthetic legacy fixture used an explicit SERIAL id.
        with self.engine.begin() as connection:
            connection.exec_driver_sql("SELECT setval(pg_get_serial_sequence('message_delivery_tasks','id'),1)")
            connection.execute(statement, {"key": None})
            connection.execute(statement, {"key": None})
            connection.execute(statement, {"key": "crm:send:example"})
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(statement, {"key": "crm:send:example"})
