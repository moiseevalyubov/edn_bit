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
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException

from sqlalchemy.orm import sessionmaker

import test_crm_postgres as postgres_tests
from app.config import settings
from app.database import Base
from app.migrations.crm_outreach_v1 import migrate_crm_outreach
from app.models import Channel, Message, MessageDeliveryTask, Portal
from app.services import telegram as tg, telegram_delivery as delivery, delivery_worker as worker
from app.routers import api
from app.schemas import ChannelCreate
from app.services import maxbot


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

    def test_concurrent_manual_registration_does_not_replace_first_callback(self):
        prepared = Barrier(2)
        callback_entered, release_callback = Event(), Event()
        callbacks = []

        async def prepare(*args):
            prepared.wait(timeout=10)

        async def callback(*args):
            callbacks.append(args)
            callback_entered.set()
            if not release_callback.wait(timeout=10):
                raise AssertionError("Test did not release callback")
            return {"code": "ok"}

        def register(sender):
            with self.Session() as db:
                try:
                    asyncio.run(api.create_channel(ChannelCreate(member_id="tg-test", name=sender,
                        sender=sender, api_key="synthetic", subject_id="99"), db, None))
                    return 200
                except HTTPException as error:
                    return error.status_code

        with patch.object(api, "require_channel_admin", AsyncMock()), \
             patch.object(api, "check_admin_context", AsyncMock()), \
             patch.object(tg, "prepare_connector", prepare), \
             patch.object(maxbot, "get_channel_profiles", AsyncMock(return_value=[])), \
             patch.object(maxbot, "set_in_message_callback", callback), \
             patch.object(maxbot, "set_status_callback", AsyncMock(return_value={"code": "ok"})), \
             ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(register, sender) for sender in ("manual-a", "manual-b")]
            try:
                self.assertTrue(callback_entered.wait(timeout=10))
                self.assertEqual(len(callbacks), 1)
            finally:
                release_callback.set()
            self.assertEqual(sorted(future.result(timeout=15) for future in futures), [200, 422])
        self.assertEqual(len(callbacks), 1)
        with self.Session() as db:
            channel = db.query(Channel).filter_by(subject_id=99).one()
            self.assertEqual(callbacks[0][2], settings.app_base_url + "/incoming/" + channel.webhook_token)

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
