import asyncio
import hashlib
import hmac
import json
import random
import ssl
import time
import urllib.parse
from pathlib import Path

import httpx
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import InvalidStatus
from websockets.typing import Origin, Subprotocol

from .session import AuthenticationError, Session
from .store import Store
from .wire import MAX_FRAME, fields, first, messages, send_packet


def webhook_signature(secret: str, body: bytes, timestamp: int) -> str:
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


class Runtime:
    def __init__(
        self,
        directory: Path,
        webhook_url: str = "",
        webhook_secret: str = "",
        allowed_conversations: set[str] | None = None,
    ):
        self.session = Session.load(directory / "session.json")
        if not self.session.user_id:
            raise ValueError("Import and verify a session before starting")
        if webhook_url:
            parsed = urllib.parse.urlsplit(webhook_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.fragment:
                raise ValueError("Invalid webhook URL")
            if parsed.username or parsed.password:
                raise ValueError("Use a signed webhook, not URL credentials")
            if len(webhook_secret) < 32:
                raise ValueError("Webhook signing secret must contain at least 32 characters")
        self.store = Store(directory, self.session.user_id)
        self.webhook_url, self.webhook_secret = webhook_url, webhook_secret
        self.allowed = allowed_conversations
        self.state = "connecting"
        self.last_sync: float | None = None
        self.last_frame: float | None = None
        self.error: str | None = None
        self.socket: ClientConnection | None = None
        self.tasks: list[asyncio.Task] = []
        self.pending: dict[int, str] = {}
        self.sign_index = 0
        self.sync_lock = asyncio.Lock()

    def status(self) -> dict:
        queued = self.store.one("SELECT count(*) AS count FROM sends WHERE status='queued'")
        return {
            "state": self.state,
            "account_id": self.session.user_id,
            "last_sync_at": self.last_sync,
            "last_frame_at": self.last_frame,
            "error": self.error,
            "webhook_enabled": bool(self.webhook_url),
            "queued_sends": queued["count"] if queued else 0,
        }

    def ingest(self, data: bytes, emit: bool = True) -> int:
        count = 0
        for message in messages(data):
            if self.allowed is not None and message["conversation_id"] not in self.allowed:
                continue
            count += self.store.ingest(message, emit=emit)
        return count

    async def sync(self) -> None:
        async with self.sync_lock:
            data = await asyncio.to_thread(self.session.inbox)
            initial = self.store.one("SELECT value FROM metadata WHERE key='initial_sync'")
            self.ingest(data, emit=bool(initial))
            with self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO metadata VALUES('initial_sync','1')")
            self.last_sync = time.time()
            if self.state != "auth_required":
                self.error = None

    async def start(self) -> None:
        self.tasks = [
            asyncio.create_task(work(), name=name)
            for work, name in [
                (self._poll, "inbox"),
                (self._connect, "socket"),
                (self._outbox, "outbox"),
                (self._webhooks, "webhooks"),
            ]
        ]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for identifier in self.pending.values():
            self.store.set_send_status(identifier, "unknown")
        self.store.close()

    async def _poll(self) -> None:
        while True:
            if self.state == "auth_required":
                await asyncio.sleep(30)
                continue
            try:
                await self.sync()
            except AuthenticationError:
                self.state, self.error = "auth_required", "session_rejected"
                if self.socket:
                    await self.socket.close()
            except Exception:
                self.error = "inbox_unavailable"
            await asyncio.sleep(15 + random.random() * 3)

    async def _receive(self, frame: bytes | str) -> None:
        self.last_frame = time.time()
        if not isinstance(frame, bytes):
            return
        try:
            root = fields(frame)
            response = fields(first(root, 8, b""))
            seq = first(response, 2)
            if first(response, 1) == 100 and seq in self.pending:
                identifier = self.pending.pop(seq)
                if first(response, 3, 0) != 0:
                    self.store.set_send_status(identifier, "rejected")
                # Only an echoed server message confirms delivery, not a local send or ACK.
            self.ingest(frame)
        except (ValueError, TypeError, UnicodeError):
            self.error = "unsupported_frame"

    async def _connect(self) -> None:
        delay = 1.0
        while True:
            if self.state == "auth_required":
                await asyncio.sleep(30)
                continue
            if not self.last_sync:
                await asyncio.sleep(1)
                continue
            connected_at = time.monotonic()
            try:
                async with websockets.connect(
                    self.session.socket_url,
                    origin=Origin("https://www.tiktok.com"),
                    user_agent_header=self.session.user_agent,
                    additional_headers={"Cookie": self.session.cookie},
                    subprotocols=[Subprotocol(x) for x in ("binary", "base64", "pbbp2")],
                    ssl=ssl.create_default_context(),
                    proxy=None,
                    max_size=MAX_FRAME,
                    max_queue=4,
                    write_limit=32768,
                    open_timeout=15,
                    close_timeout=3,
                    ping_interval=20,
                    ping_timeout=10,
                ) as socket:
                    self.socket = socket
                    self.state = "connected"
                    await socket.send("hi")
                    while self.state != "auth_required":
                        try:
                            frame = await asyncio.wait_for(socket.recv(), timeout=15)
                            await self._receive(frame)
                        except TimeoutError:
                            await socket.send("hi")
            except InvalidStatus as exc:
                if exc.response.status_code in (401, 403):
                    self.state, self.error = "auth_required", "session_rejected"
                else:
                    self.error = "socket_unavailable"
            except Exception:
                self.error = "socket_unavailable"
            finally:
                self.socket = None
                for identifier in self.pending.values():
                    self.store.set_send_status(identifier, "unknown")
                self.pending.clear()
                if self.state != "auth_required":
                    self.state = "connecting"
            if time.monotonic() - connected_at > 60:
                delay = 1
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 60)

    async def _outbox(self) -> None:
        while True:
            await asyncio.sleep(3)
            with self.store.db:
                self.store.db.execute(
                    "UPDATE sends SET status='unknown',updated_at=? WHERE status='submitted' AND updated_at<?",
                    (time.time(), time.time() - 60),
                )
            for seq, identifier in list(self.pending.items()):
                record = self.store.send(identifier)
                if not record or record["status"] not in {"sending", "submitted"}:
                    self.pending.pop(seq, None)
            if self.state != "connected" or not self.socket or not self.last_sync:
                continue
            if time.time() - self.last_sync > 60:
                continue
            send = self.store.one(
                "SELECT * FROM sends WHERE status='queued' ORDER BY created_at LIMIT 1"
            )
            if not send:
                continue
            conversation = self.store.one(
                "SELECT * FROM conversations WHERE id=?", (send["conversation_id"],)
            )
            if not conversation or (
                self.allowed is not None and conversation["id"] not in self.allowed
            ):
                self.store.set_send_status(send["id"], "rejected")
                continue
            self.store.set_send_status(send["id"], "sending")
            self.sign_index += 1
            try:
                packet = send_packet(
                    self.session.template, conversation, send["text"], send["id"], self.sign_index
                )
                seq = first(fields(packet), 2)
                self.pending[seq] = send["id"]
                await self.socket.send(packet)
                self.store.set_send_status(send["id"], "submitted")
            except Exception:
                self.store.set_send_status(send["id"], "unknown")
                self.error = "send_unconfirmed"
            # Never automatically retry submitted, rejected or uncertain sends.

    async def _webhooks(self) -> None:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            while True:
                await asyncio.sleep(1)
                if not self.webhook_url:
                    continue
                event = self.store.one(
                    "SELECT * FROM events WHERE status='pending' AND next_at<=? ORDER BY row_id LIMIT 1",
                    (time.time(),),
                )
                if not event:
                    continue
                payload = {
                    "id": event["id"],
                    "type": event["type"],
                    "created_at": event["created_at"],
                    "data": json.loads(event["data"]),
                }
                body = json.dumps(payload, separators=(",", ":")).encode()
                signature = webhook_signature(self.webhook_secret, body, int(time.time()))
                delivered = False
                try:
                    async with client.stream(
                        "POST",
                        self.webhook_url,
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-TTDM-Signature": signature,
                            "X-TTDM-Event-ID": event["id"],
                        },
                    ) as response:
                        delivered = 200 <= response.status_code < 300
                except Exception:
                    pass
                attempts = event["attempts"] + 1
                status = "delivered" if delivered else "dead" if attempts >= 12 else "pending"
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE events SET status=?,attempts=?,next_at=? WHERE id=?",
                        (
                            status,
                            attempts,
                            time.time() + min(2**attempts, 3600) + random.random(),
                            event["id"],
                        ),
                    )
