"""Queue/lock races on an explicitly isolated PostgreSQL database only."""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from threading import Barrier, Event
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

import test_crm_postgres as postgres_tests
from app.config import settings
from app.database import Base
from app.migrations.crm_outreach_v1 import migrate_crm_outreach
from app.models import Channel, Message, MessageDeliveryTask, Portal
from app.services import telegram as tg, telegram_delivery as delivery, delivery_worker as worker


@unittest.skipUnless(os.environ.get("CRM_TEST_POSTGRES_URL"), "Explicit isolated test PostgreSQL not configured")
class TelegramPostgresTests(unittest.TestCase):
    def setUp(self):
        postgres_tests.PostgreSqlTests.setUp(self)
        migrate_crm_outreach(self.engine, Base.metadata)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        with self.Session() as db:
            portal = Portal(member_id="tg-test", access_token="synthetic", refresh_token="synthetic",
                            client_endpoint="https://portal.invalid/rest/", open_line_id="7", app_token="synthetic")
            db.add(portal)
            db.flush()
            channel = Channel(portal_id=portal.id, name="Telegram", sender="tg", subject_id=78,
                              channel_type="TELEGRAM", api_key="synthetic", is_active=True)
            db.add(channel)
            db.commit()
            self.channel_id, self.portal_id = channel.id, portal.id
        self.flag = patch.object(settings, "telegram_enabled", True)
        self.flag.start()
        self.addCleanup(self.flag.stop)

    def tearDown(self):
        postgres_tests.PostgreSqlTests.tearDown(self)

    def incoming(self):
        with self.Session() as db:
            delivery.accept_incoming(db, db.get(Channel, self.channel_id), {
                "id": "100", "subject": "tg", "subjectId": 78,
                "subscriber": {"identifiers": [{"type": "TELEGRAM_ID", "value": "123"}]},
                "messageContent": {"type": "TEXT", "text": "Hello"},
            })

    def outgoing(self):
        with self.Session() as db:
            delivery.accept_outgoing(db, db.get(Portal, self.portal_id), {
                "data": {"CONNECTOR": tg.CONNECTOR, "LINE": "7", "MESSAGES": [{
                    "chat": {"id": tg.chat_id(db.get(Channel, self.channel_id), "123")},
                    "im": {"chat_id": "41", "message_id": "501"}, "message": {"text": "Reply"},
                }]},
            })

    def test_concurrent_incoming_and_outgoing_have_one_durable_operation(self):
        for receive in (self.incoming, self.outgoing):
            barrier = Barrier(2)
            def attempt():
                barrier.wait(timeout=10)
                try:
                    receive()
                except tg.TelegramError as exc:
                    self.assertTrue(exc.retryable)
                    receive()  # Simulate upstream retry after the winner committed.
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(attempt) for _ in range(2)]
                for result in futures:
                    result.result(timeout=20)
        with self.Session() as db:
            self.assertEqual(db.query(Message).count(), 2)
            self.assertEqual(db.query(MessageDeliveryTask).filter_by(task_type=tg.RECEIVE).count(), 1)
            self.assertEqual(db.query(MessageDeliveryTask).filter_by(task_type=tg.SEND).count(), 1)

    def test_live_send_is_not_reset_by_stale_scan_and_cannot_be_claimed_twice(self):
        self.incoming()
        self.outgoing()
        with self.Session() as db:
            db.query(MessageDeliveryTask).filter_by(task_type=tg.RECEIVE).update({"status": "sent"})
            task = db.query(MessageDeliveryTask).filter_by(task_type=tg.SEND).one()
            task_id = task.id
            db.commit()
        started, release = Event(), Event()
        sends = []
        async def hold_send(*args):
            sends.append(args)
            started.set()
            if not release.wait(timeout=10):
                raise AssertionError("test release timeout")
        with patch.object(worker, "SessionLocal", self.Session), patch.object(tg, "send_text", hold_send), ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(lambda: asyncio.run(worker._process_one_task()))
            try:
                self.assertTrue(started.wait(timeout=10))
                with self.Session() as db:
                    db.get(MessageDeliveryTask, task_id).updated_at = datetime.utcnow() - timedelta(minutes=10)
                    db.commit()
                asyncio.run(worker._reset_stale_tasks())
                with self.Session() as db:
                    self.assertEqual(db.get(MessageDeliveryTask, task_id).status, "processing")
                self.assertFalse(asyncio.run(worker._process_one_task()))
                # A synchronous upstream receipt must not wait for the outgoing
                # channel row lock (FK KEY SHARE is compatible with NO KEY UPDATE).
                with self.Session() as db:
                    message = db.query(Message).filter_by(direction="outgoing").one()
                    delivery.accept_status(db, db.get(Channel, self.channel_id),
                                           {"requestId": message.edna_request_id, "status": "SENT"})
                self.assertTrue(asyncio.run(worker._process_one_task()))  # Status defers on message advisory lock.
            finally:
                release.set()
            running.result(timeout=15)
        self.assertEqual(len(sends), 1)
        with self.Session() as db:
            self.assertEqual(db.get(MessageDeliveryTask, task_id).status, "sent")
