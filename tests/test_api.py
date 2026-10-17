import importlib.util
import json
import os
import time
from pathlib import Path

from conftest import CONVERSATION, TOKEN, message_wire
from fastapi.testclient import TestClient

from tiktok_dm_api.app import create_app
from tiktok_dm_api.runtime import webhook_signature


def client(runtime):
    return TestClient(create_app(runtime, TOKEN, run_workers=False))


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_api_auth_and_no_secret_exposure(runtime):
    c = client(runtime)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/v1/status").status_code == 401
    assert c.get("/openapi.json").status_code == 401
    response = c.get("/v1/status", headers=auth())
    assert response.status_code == 200
    assert "synthetic-test-value" not in response.text
    assert "socket_url" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert c.get("/readyz", headers=auth()).status_code == 503


def test_openapi_and_pagination(runtime):
    runtime.ingest(message_wire())
    c = client(runtime)
    schema = c.get("/openapi.json", headers=auth()).json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    response = c.get(f"/v1/conversations/{CONVERSATION}/messages", headers=auth())
    assert response.status_code == 200
    cursor = response.json()["next_cursor"]
    assert len(response.json()["items"]) == 1
    assert not c.get(
        f"/v1/conversations/{CONVERSATION}/messages?after={cursor}", headers=auth()
    ).json()["items"]
    assert c.get("/v1/conversations?limit=1000", headers=auth()).status_code == 422


def test_send_requires_key_and_existing_inbound(runtime):
    c = client(runtime)
    payload = {"conversation_id": CONVERSATION, "text": "hello"}
    assert c.post("/v1/messages", headers=auth(), json=payload).status_code == 422
    headers = {**auth(), "Idempotency-Key": "unique"}
    assert c.post("/v1/messages", headers=headers, json=payload).status_code == 403
    runtime.ingest(message_wire())
    first = c.post("/v1/messages", headers=headers, json=payload)
    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    again = c.post("/v1/messages", headers=headers, json=payload)
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert (
        c.post("/v1/messages", headers=headers, json={**payload, "text": "different"}).status_code
        == 409
    )


def test_bounds_and_validation_do_not_echo_input(runtime):
    c = client(runtime)
    runtime.ingest(message_wire())
    headers = {**auth(), "Idempotency-Key": "unique"}
    for text in ("", " ", "x" * 4001, "\x00"):
        response = c.post(
            "/v1/messages", headers=headers, json={"conversation_id": CONVERSATION, "text": text}
        )
        assert response.status_code == 422
        assert response.json() == {"error": "invalid_request"}
    assert c.post("/v1/messages", headers=auth(), content=b"x" * 32769).status_code == 413


def test_events_and_dead_letter_retry(runtime):
    runtime.ingest(message_wire())
    c = client(runtime)
    event = c.get("/v1/events", headers=auth()).json()["items"][0]
    assert event["type"] == "message.received"
    assert isinstance(event["data"], dict)
    assert c.post(f"/v1/events/{event['id']}/retry", headers=auth()).status_code == 404
    with runtime.store.db:
        runtime.store.db.execute("UPDATE events SET status='dead'")
    assert c.post(f"/v1/events/{event['id']}/retry", headers=auth()).status_code == 200


def test_receiver_example_verifies_signature_and_keeps_inbox_private(tmp_path, monkeypatch):
    secret = "synthetic-example-webhook-secret-00000"
    path = tmp_path / "inbox.sqlite3"
    monkeypatch.setenv("TTDM_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("WEBHOOK_STATE", str(path))
    spec = importlib.util.spec_from_file_location(
        "receiver_example", Path(__file__).parents[1] / "examples/webhook.py"
    )
    module = importlib.util.module_from_spec(spec)
    old_mask = os.umask(0o077)
    try:
        spec.loader.exec_module(module)
        c = TestClient(module.app)
        body = json.dumps(
            {"id": "synthetic-event", "type": "message.received", "data": {}}
        ).encode()
        headers = {
            "X-TTDM-Event-ID": "synthetic-event",
            "X-TTDM-Signature": webhook_signature(secret, body, int(time.time())),
        }
        assert c.post("/webhook", content=body, headers=headers).status_code == 200
        assert c.post("/webhook", content=body, headers=headers).status_code == 200
        assert module.db.execute("SELECT count(*) FROM received").fetchone()[0] == 1
        assert path.stat().st_mode & 0o777 == 0o600
        headers["X-TTDM-Signature"] = webhook_signature(secret, body, 1)
        assert c.post("/webhook", content=body, headers=headers).status_code == 401
    finally:
        module.db.close()
        os.umask(old_mask)
