import asyncio
import hashlib
import hmac
import json

import pytest
from conftest import message_wire

from tiktok_dm_api.runtime import webhook_signature
from tiktok_dm_api.session import Session, fetch_export, validate_url
from tiktok_dm_api.wire import messages


@pytest.mark.parametrize(
    "kind,url",
    [
        ("inbox", "http://im-api.tiktok.com/v2/message/get_by_user_init"),
        ("inbox", "https://im-api.tiktok.com.evil.invalid/v2/message/get_by_user_init"),
        ("inbox", "https://im-api.tiktok.com:444/v2/message/get_by_user_init"),
        ("inbox", "https://user@im-api.tiktok.com/v2/message/get_by_user_init"),
        ("inbox", "https://im-api.tiktok.com/v2/message/get_by_user_init#fragment"),
        ("socket", "wss://evil.invalid/ws/v2"),
        ("socket", "wss://im-ws.tiktok.com/other"),
    ],
)
def test_destination_allowlist(kind, url):
    with pytest.raises(ValueError):
        validate_url(url, kind)


def test_permissions_and_redacted_repr(runtime, tmp_path):
    path = tmp_path / "private" / "session.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "synthetic-test-value" not in repr(runtime.session)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        Session.load(path)


def test_safe_capture_parser(tmp_path):
    capture = tmp_path / "capture"
    data = {"headers": {"cookie": "synthetic"}, "body": "\x00\xff", "method": "POST"}
    capture.write_text('fetch("https://example.invalid", ' + json.dumps(data) + ");")
    capture.chmod(0o600)
    assert fetch_export(capture)["body"].encode("latin1") == b"\x00\xff"
    capture.write_text('fetch("https://example.invalid", {}); malicious();')
    with pytest.raises(ValueError):
        fetch_export(capture)


def test_webhook_signature_authenticates_timestamp_and_raw_body():
    body, timestamp, secret = b'{"hello":"world"}', 100, "synthetic-signing-secret"
    expected = hmac.new(secret.encode(), b"100." + body, hashlib.sha256).hexdigest()
    assert webhook_signature(secret, body, timestamp) == f"t=100,v1={expected}"
    assert webhook_signature(secret, body, timestamp + 1) != f"t=100,v1={expected}"


def test_first_sync_does_not_replay_old_messages(runtime, monkeypatch):
    monkeypatch.setattr(runtime.session, "inbox", lambda: message_wire())
    asyncio.run(runtime.sync())
    assert runtime.store.many("SELECT * FROM messages")
    assert not runtime.store.many("SELECT * FROM events")
    monkeypatch.setattr(runtime.session, "inbox", lambda: message_wire(msg_id=124, text="new"))
    asyncio.run(runtime.sync())
    assert len(runtime.store.many("SELECT * FROM events")) == 1


def test_account_inference(runtime):
    first = message_wire()
    runtime.session.user_id = ""
    with pytest.raises(ValueError, match="one conversation"):
        runtime.session.identify(first)
    runtime.session.user_id = "100"
    runtime.session.identify(first)
    assert next(messages(first))["sender_id"] == "200"
