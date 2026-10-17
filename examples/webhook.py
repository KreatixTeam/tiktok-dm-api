"""Signed webhook receiver. Put your application logic in handle_event."""

import hashlib
import hmac
import json
import os
import sqlite3
import time

from fastapi import FastAPI, HTTPException, Request

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
secret = os.environ["TTDM_WEBHOOK_SECRET"].encode()
db = sqlite3.connect(os.getenv("WEBHOOK_STATE", "webhook.sqlite3"), check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS received(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
db.commit()


@app.post("/webhook")
async def handle_event(request: Request):
    chunks = []
    length = 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > 65536:
            raise HTTPException(413)
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        values = dict(part.split("=", 1) for part in request.headers["X-TTDM-Signature"].split(","))
        timestamp = int(values["t"])
        expected = hmac.new(
            secret, str(timestamp).encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        if abs(time.time() - timestamp) > 300 or not hmac.compare_digest(expected, values["v1"]):
            raise ValueError()
        event = json.loads(body)
        if event["id"] != request.headers["X-TTDM-Event-ID"]:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "Invalid webhook signature") from None
    # This durable inbox is idempotent. Process new rows in your application's worker.
    with db:
        db.execute("INSERT OR IGNORE INTO received VALUES(?,?)", (event["id"], body.decode()))
    return {"accepted": True}
