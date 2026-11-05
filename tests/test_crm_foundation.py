"""Schema and legacy-worker regressions; no app.main import or real network."""
import os
import unittest

# Refuse direct discovery from the repository, which could load its .env.
if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py to avoid project credentials")

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import Settings, settings
from app.database import Base
from app.migrations.crm_outreach_v1 import migrate_crm_outreach
from app.models import (
    Channel, CrmMessageProvider, CrmMessageStatusEvent, CrmOutgoingMessage,
    Message, MessageDeliveryTask, Portal,
)
from app.services import delivery_worker as worker
from app.services.task_types import CRM_TASK_TYPES, SEND_CRM_TO_EDNA, SYNC_CRM_PROVIDER


def create_legacy_schema(engine):
    sql = Path(__file__).with_name("legacy_schema.sql").read_text(encoding="utf-8")
    if engine.dialect.name == "postgresql":
        sql = sql.replace("id INTEGER PRIMARY KEY", "id SERIAL PRIMARY KEY")
    with engine.begin() as connection:
        for statement in sql.split(";"):
            if statement.strip():
                connection.exec_driver_sql(statement)
        connection.execute(text(
            "INSERT INTO portals (id,member_id,client_endpoint,access_token,refresh_token) "
            "VALUES (1,'legacy-portal','https://legacy.invalid/rest/','existing-ciphertext','unchanged-ciphertext')"
        ))
        connection.execute(text(
            "INSERT INTO channels (id,portal_id,name,api_key,sender,webhook_token,is_active) "
            "VALUES (1,1,'Existing channel','existing-api-ciphertext','sender-1','test-webhook',TRUE)"
        ))
        connection.execute(text(
            "INSERT INTO message_delivery_tasks "
            "(id,channel_id,task_type,direction,payload,status,retry_count,next_attempt_at,created_at,updated_at) "
            "VALUES (1,1,'send_to_edna','outgoing',:payload,'pending',2,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        ), {"payload": '{"original":true}'})
        Base.metadata.tables["messages"].create(bind=connection)
        connection.execute(text(
            "INSERT INTO messages (id,channel_id,direction,text,content_type,subscriber_identifier) "
            "VALUES (1,1,'incoming','Keep this history','TEXT','existing-client')"
        ))


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="crm-db-")
        self.engine = create_engine("sqlite:///" + str(Path(self.directory.name) / "test.sqlite"))

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        # Match production SessionLocal: queries must not hide missing flushes.
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def seed(self):
        migrate_crm_outreach(self.engine, Base.metadata)
        with self.Session() as db:
            portal = Portal(
                member_id="test-portal", client_endpoint="https://portal.invalid/rest/",
                access_token="synthetic-access", refresh_token="synthetic-refresh",
            )
            db.add(portal)
            db.flush()
            channel = Channel(
                portal_id=portal.id, name="Test MAX", api_key="synthetic-key", sender="test-sender",
                channel_type="MAX", is_active=True,
            )
            db.add(channel)
            db.commit()
            self.portal_id, self.channel_id = portal.id, channel.id


class MigrationTests(DatabaseCase):
    def test_fresh_database_and_repeat_start(self):
        migrate_crm_outreach(self.engine, Base.metadata)
        migrate_crm_outreach(self.engine, Base.metadata)
        self.assertEqual(set(inspect(self.engine).get_table_names()), set(Base.metadata.tables))
        self.seed()
        with self.Session() as db:
            self.assertFalse(db.get(Portal, self.portal_id).crm_outreach_enabled)

    def test_upgrade_keeps_old_rows_secrets_queue_and_history(self):
        create_legacy_schema(self.engine)
        with self.engine.connect() as connection:
            before = {name: connection.execute(text("SELECT * FROM " + name)).all()
                      for name in ("portals", "channels", "messages", "message_delivery_tasks")}
        migrate_crm_outreach(self.engine, Base.metadata)
        migrate_crm_outreach(self.engine, Base.metadata)
        with self.engine.connect() as connection:
            for name, rows in before.items():
                after = connection.execute(text("SELECT * FROM " + name)).all()
                self.assertEqual([tuple(row[:len(rows[0])]) for row in after], [tuple(row) for row in rows])
            self.assertFalse(connection.execute(text("SELECT crm_outreach_enabled FROM portals")).scalar_one())
            self.assertIsNone(connection.execute(text("SELECT dedup_key FROM message_delivery_tasks")).scalar_one())

    def test_multiple_legacy_null_keys_and_one_crm_operation(self):
        self.seed()
        with self.Session() as db:
            for key in (None, None, "crm:send:1"):
                db.add(MessageDeliveryTask(channel_id=self.channel_id, task_type="send_to_edna",
                                           direction="outgoing", payload="{}", dedup_key=key))
            db.commit()
            db.add(MessageDeliveryTask(channel_id=self.channel_id, task_type="send_to_edna",
                                       direction="outgoing", payload="{}", dedup_key="crm:send:1"))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()
            self.assertEqual(db.query(MessageDeliveryTask).count(), 3)

    def test_provider_code_is_unique_within_portal(self):
        self.seed()
        with self.Session() as db:
            db.add(CrmMessageProvider(portal_id=self.portal_id, channel_id=self.channel_id, provider_code="edna_max_1"))
            db.commit()
            second_channel = Channel(portal_id=self.portal_id, name="Second MAX", api_key="synthetic-key", sender="sender-2")
            db.add(second_channel)
            db.commit()
            db.add(CrmMessageProvider(portal_id=self.portal_id, channel_id=second_channel.id, provider_code="edna_max_1"))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()
            db.add(CrmMessageProvider(portal_id=self.portal_id, channel_id=self.channel_id, provider_code="edna_max_other"))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_same_code_and_message_id_are_independent_between_portals(self):
        self.seed()
        with self.Session() as db:
            second_portal = Portal(member_id="other-portal", client_endpoint="https://other.invalid/rest/",
                                   access_token="synthetic-access", refresh_token="synthetic-refresh")
            db.add(second_portal)
            db.flush()
            second_channel = Channel(portal_id=second_portal.id, name="Other MAX", api_key="synthetic-key", sender="sender-2")
            db.add(second_channel)
            db.flush()
            for portal_id, channel_id in ((self.portal_id, self.channel_id), (second_portal.id, second_channel.id)):
                db.add(CrmMessageProvider(portal_id=portal_id, channel_id=channel_id, provider_code="edna_max_1"))
                db.add(CrmOutgoingMessage(portal_id=portal_id, channel_id=channel_id, provider_code="edna_max_1",
                                          bitrix_message_id="same-crm-id", edna_out_message_id="same-external-id",
                                          text="Test", recipient_phone="+70000000000"))
            db.commit()
            self.assertEqual(db.query(CrmMessageProvider).count(), 2)
            self.assertEqual(db.query(CrmOutgoingMessage).count(), 2)

    def test_message_and_queue_can_rollback_together(self):
        self.seed()
        with self.Session() as db:
            message = CrmOutgoingMessage(
                portal_id=self.portal_id, channel_id=self.channel_id, provider_code="edna_max_1",
                bitrix_message_id="crm-1", text="Test", recipient_phone="+70000000000", edna_out_message_id="request-1",
            )
            db.add(message)
            db.flush()
            db.add(MessageDeliveryTask(channel_id=self.channel_id, task_type=SEND_CRM_TO_EDNA,
                                       direction="outgoing", payload=json.dumps({"crm_message_id": message.id}),
                                       dedup_key="crm:send:1"))
            db.flush()
            db.rollback()
        with self.Session() as db:
            self.assertEqual(db.query(CrmOutgoingMessage).count(), 0)
            self.assertEqual(db.query(MessageDeliveryTask).count(), 0)

    def test_duplicate_crm_message_and_status_event_rejected(self):
        self.seed()
        values = dict(portal_id=self.portal_id, channel_id=self.channel_id, provider_code="edna_max_1",
                      bitrix_message_id="crm-1", text="Test", recipient_phone="+70000000000", edna_out_message_id="req-1")
        with self.Session() as db:
            message = CrmOutgoingMessage(**values)
            db.add(message)
            db.commit()
            db.add(CrmOutgoingMessage(**{**values, "edna_out_message_id": "req-other"}))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()
            for _ in range(2):
                db.add(CrmMessageStatusEvent(crm_message_id=message.id, status="READ", request_id="req-1", fingerprint="a" * 64))
                if _ == 0:
                    db.commit()
                else:
                    with self.assertRaises(IntegrityError):
                        db.commit()
                    db.rollback()

    def test_flags_default_off_without_loading_env(self):
        config = Settings(_env_file=None)
        self.assertFalse(config.crm_outreach_enabled)
        self.assertEqual(config.bitrix_expected_app_code, "")


class WorkerTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.session_patch = patch.object(worker, "SessionLocal", self.Session)
        self.session_patch.start()

    def tearDown(self):
        self.session_patch.stop()
        super().tearDown()

    def task(self, kind, payload="{}", status="pending"):
        with self.Session() as db:
            row = MessageDeliveryTask(
                channel_id=self.channel_id, task_type=kind, direction="outgoing", payload=payload,
                status=status, next_attempt_at=datetime.utcnow() - timedelta(seconds=1),
                updated_at=datetime.utcnow() - timedelta(seconds=600),
            )
            db.add(row)
            db.commit()
            return row.id

    def status(self, task_id):
        with self.Session() as db:
            return db.get(MessageDeliveryTask, task_id).status

    async def test_legacy_dispatch_is_unchanged(self):
        incoming = self.task("send_to_bitrix")
        outgoing = self.task("send_to_edna")
        with patch.object(worker, "_execute_incoming", new_callable=AsyncMock) as receive, \
             patch.object(worker, "_execute_outgoing", new_callable=AsyncMock) as send:
            self.assertTrue(await worker._process_one_task())
            self.assertTrue(await worker._process_one_task())
            receive.assert_awaited_once_with(self.channel_id, {})
            send.assert_awaited_once_with(self.channel_id, {})
        self.assertEqual((self.status(incoming), self.status(outgoing)), ("sent", "sent"))

    async def test_reserved_crm_tasks_do_not_send_or_starve_legacy_tasks(self):
        reserved = [self.task(SEND_CRM_TO_EDNA)]
        legacy = self.task("send_to_edna")
        with patch.object(worker, "_execute_outgoing", new_callable=AsyncMock) as send, \
             patch.object(worker.settings, "crm_outreach_enabled", False):
            self.assertTrue(await worker._process_one_task())
            self.assertFalse(await worker._process_one_task())
            send.assert_awaited_once_with(self.channel_id, {})
        self.assertEqual(self.status(legacy), "sent")
        self.assertEqual([self.status(task_id) for task_id in reserved], ["pending"] * len(reserved))

    async def test_unknown_task_is_not_an_outgoing_message(self):
        task_id = self.task("misspelled-operation")
        with patch.object(worker, "_execute_outgoing", new_callable=AsyncMock) as send:
            await worker._process_one_task()
            send.assert_not_awaited()
        self.assertEqual(self.status(task_id), "dead")

    async def test_malformed_legacy_payload_finishes_without_network(self):
        task_id = self.task("send_to_edna", "not-json")
        with patch.object(worker, "_execute_outgoing", new_callable=AsyncMock) as send, \
             patch.object(worker, "notify_outgoing_undelivered", new_callable=AsyncMock):
            await worker._process_one_task()
            send.assert_not_awaited()
        self.assertEqual(self.status(task_id), "dead")

    async def test_legacy_timeout_still_uses_existing_retry_policy(self):
        import httpx
        task_id = self.task("send_to_edna")
        with patch.object(worker, "_execute_outgoing", new_callable=AsyncMock, side_effect=httpx.ReadTimeout("synthetic timeout")):
            await worker._process_one_task()
        with self.Session() as db:
            task = db.get(MessageDeliveryTask, task_id)
            self.assertEqual((task.status, task.retry_count), ("pending", 1))
            self.assertGreater(task.next_attempt_at, datetime.utcnow())

    async def test_stale_legacy_task_returns_to_pending(self):
        task_id = self.task("send_to_edna", status="processing")
        await worker._reset_stale_tasks()
        self.assertEqual(self.status(task_id), "pending")

    async def test_stale_crm_send_is_recovered_without_blind_resend(self):
        for state, started, expected in [
            ("queued", False, "pending"), ("sending", True, "uncertain"),
            ("accepted", True, "sent"), ("failed", True, "failed"),
        ]:
            with self.subTest(state=state), self.Session() as db:
                message = CrmOutgoingMessage(
                    portal_id=self.portal_id, channel_id=self.channel_id, provider_code="edna_max_1",
                    bitrix_message_id="crm-" + state, text="Test", recipient_phone="+70000000000",
                    edna_out_message_id="request-" + state, send_state=state,
                    attempt_started_at=datetime.utcnow() if started else None,
                )
                db.add(message)
                db.commit()
                message_id = message.id
                task_id = self.task(SEND_CRM_TO_EDNA, json.dumps({"crm_message_id": message_id}), "processing")
                await worker._reset_stale_tasks()
                self.assertEqual(self.status(task_id), expected)
                db.expire_all()
                self.assertEqual(db.get(CrmOutgoingMessage, message_id).send_state,
                                 "uncertain" if expected == "uncertain" else state)

    async def test_stale_crm_missing_reference_cannot_send(self):
        task_id = self.task(SEND_CRM_TO_EDNA, "{}", "processing")
        await worker._reset_stale_tasks()
        self.assertEqual(self.status(task_id), "uncertain")
