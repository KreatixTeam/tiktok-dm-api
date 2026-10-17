import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .session import secure_directory
from .wire import DM_ID


class Conflict(Exception):
    pass


class Store:
    def __init__(self, directory: Path, own_id: str):
        secure_directory(directory)
        self.own_id = own_id
        path = directory / "state.sqlite3"
        if path.is_symlink():
            raise ValueError("Database symlink refused")
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            PRAGMA cache_size=-2048;
            CREATE TABLE IF NOT EXISTS conversations(
                id TEXT PRIMARY KEY, short_id TEXT NOT NULL, can_reply INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(
                row_id INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                conversation_id TEXT NOT NULL, sender_id TEXT NOT NULL, text TEXT,
                content_type TEXT NOT NULL, client_id TEXT, timestamp INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, row_id);
            CREATE TABLE IF NOT EXISTS sends(
                id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                conversation_id TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL,
                server_message_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS sends_status ON sends(status, created_at);
            CREATE TABLE IF NOT EXISTS events(
                row_id INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS events_pending ON events(status, next_at);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        account = self.one("SELECT value FROM metadata WHERE key='account_id'")
        if account and account["value"] != own_id:
            self.db.close()
            raise ValueError("Use a separate data directory for each TikTok account")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('account_id',?)", (own_id,))
            self.db.execute("UPDATE sends SET status='unknown' WHERE status='sending'")
            self.db.execute("UPDATE sends SET status='unknown' WHERE status='submitted'")

    def close(self):
        self.db.close()

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def many(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, args)]

    def _event(self, kind: str, data: dict) -> None:
        now = time.time()
        self.db.execute(
            "INSERT INTO events(id,type,data,next_at,created_at) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), kind, json.dumps(data, separators=(",", ":")), now, now),
        )

    def ingest(self, message: dict, emit: bool = True) -> bool:
        match = DM_ID.fullmatch(message["conversation_id"])
        if (
            not match
            or self.own_id not in match.groups()
            or message["sender_id"] not in match.groups()
        ):
            return False
        with self.db:
            self.db.execute(
                """INSERT INTO conversations VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET short_id=excluded.short_id,
                can_reply=MAX(can_reply,excluded.can_reply),updated_at=excluded.updated_at""",
                (
                    message["conversation_id"],
                    message["short_id"],
                    int(message["sender_id"] != self.own_id),
                    time.time(),
                ),
            )
            inserted = (
                self.db.execute(
                    """INSERT OR IGNORE INTO messages
                (id,conversation_id,sender_id,text,content_type,client_id,timestamp)
                VALUES(:id,:conversation_id,:sender_id,:text,:content_type,:client_id,:timestamp)""",
                    message,
                ).rowcount
                == 1
            )
            if inserted and emit:
                self._event(
                    "message.received" if message["sender_id"] != self.own_id else "message.sent",
                    {k: v for k, v in message.items() if k != "short_id"},
                )
            if message["sender_id"] == self.own_id and message.get("client_id"):
                changed = self.db.execute(
                    """UPDATE sends SET status='confirmed',server_message_id=?,updated_at=?
                    WHERE id=? AND conversation_id=? AND status!='confirmed'""",
                    (message["id"], time.time(), message["client_id"], message["conversation_id"]),
                ).rowcount
                if changed:
                    self._event("send.confirmed", self.send(message["client_id"]) or {})
        return inserted

    def send(self, identifier: str) -> dict | None:
        return self.one(
            """SELECT id,conversation_id,text,status,server_message_id,created_at,updated_at
            FROM sends WHERE id=?""",
            (identifier,),
        )

    def enqueue(self, conversation: str, text: str, key: str) -> tuple[dict, bool]:
        fingerprint = hashlib.sha256(json.dumps([conversation, text]).encode()).hexdigest()
        with self.db:
            old = self.one("SELECT * FROM sends WHERE idem=?", (key,))
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("Idempotency key already used with a different request")
                return self.send(old["id"]) or {}, False
            conv = self.one("SELECT * FROM conversations WHERE id=?", (conversation,))
            if not conv or not conv["can_reply"]:
                raise ValueError(
                    "Only existing conversations with an incoming message can be replied to"
                )
            if (
                self.db.execute("SELECT count(*) FROM sends WHERE status='queued'").fetchone()[0]
                >= 100
            ):
                raise Conflict("Outbound queue full")
            identifier, now = str(uuid.uuid4()), time.time()
            self.db.execute(
                "INSERT INTO sends VALUES(?,?,?,?,?,?,NULL,?,?)",
                (identifier, key, fingerprint, conversation, text, "queued", now, now),
            )
        return self.send(identifier) or {}, True

    def set_send_status(self, identifier: str, status: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sends SET status=?,updated_at=? WHERE id=? AND status!='confirmed'",
                (status, time.time(), identifier),
            )
