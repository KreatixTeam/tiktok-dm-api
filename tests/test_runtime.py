import asyncio
import time

from conftest import CONVERSATION, message_wire
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from tiktok_dm_api.wire import blob, number


def test_disconnect_reconnects_but_does_not_retry_uncertain_send(runtime, monkeypatch):
    runtime.ingest(message_wire())
    send, _ = runtime.store.enqueue(CONVERSATION, "hello", "reconnect")
    runtime.store.set_send_status(send["id"], "submitted")
    runtime.pending[123] = send["id"]
    runtime.last_sync = time.time()
    original_sleep = asyncio.sleep
    attempts = []

    async def scenario():
        connected_twice = asyncio.Event()

        class Socket:
            async def __aenter__(self):
                attempts.append(1)
                if len(attempts) == 2:
                    connected_twice.set()
                return self

            async def __aexit__(self, *args):
                pass

            async def send(self, data):
                assert data == "hi"

            async def recv(self):
                if len(attempts) == 1:
                    raise OSError("synthetic disconnect")
                await asyncio.Future()

        async def fast_sleep(delay):
            await original_sleep(0)

        monkeypatch.setattr("tiktok_dm_api.runtime.websockets.connect", lambda *a, **kw: Socket())
        monkeypatch.setattr("tiktok_dm_api.runtime.asyncio.sleep", fast_sleep)
        task = asyncio.create_task(runtime._connect())
        try:
            await asyncio.wait_for(connected_twice.wait(), 2)
            assert runtime.store.send(send["id"])["status"] == "unknown"
            assert not runtime.pending
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(attempts) == 2


def test_rejected_authentication_does_not_keep_reconnecting(runtime, monkeypatch):
    runtime.last_sync = time.time()
    original_sleep = asyncio.sleep
    attempts = []

    def refused(*args, **kwargs):
        attempts.append(1)
        raise InvalidStatus(Response(403, "Forbidden", Headers()))

    async def fast_sleep(delay):
        await original_sleep(0)

    async def scenario():
        monkeypatch.setattr("tiktok_dm_api.runtime.websockets.connect", refused)
        monkeypatch.setattr("tiktok_dm_api.runtime.asyncio.sleep", fast_sleep)
        task = asyncio.create_task(runtime._connect())
        try:
            for _ in range(10):
                await original_sleep(0)
            assert runtime.state == "auth_required"
            assert len(attempts) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_positive_ack_is_not_delivery_confirmation(runtime):
    runtime.ingest(message_wire())
    send, _ = runtime.store.enqueue(CONVERSATION, "hello", "ack")
    runtime.store.set_send_status(send["id"], "submitted")
    runtime.pending[42] = send["id"]
    ack = blob(8, number(1, 100) + number(2, 42) + number(3, 0))
    asyncio.run(runtime._receive(ack))
    assert runtime.store.send(send["id"])["status"] == "submitted"
    assert not runtime.pending


def test_negative_ack_rejects_send(runtime):
    runtime.ingest(message_wire())
    send, _ = runtime.store.enqueue(CONVERSATION, "hello", "nack")
    runtime.store.set_send_status(send["id"], "submitted")
    runtime.pending[42] = send["id"]
    ack = blob(8, number(1, 100) + number(2, 42) + number(3, 500))
    asyncio.run(runtime._receive(ack))
    assert runtime.store.send(send["id"])["status"] == "rejected"
