import pytest
from conftest import CONVERSATION, message_wire

from tiktok_dm_api.store import Conflict, Store
from tiktok_dm_api.wire import messages


def incoming(runtime):
    runtime.ingest(message_wire())
    return runtime.store


def test_ingest_is_durable_and_deduplicated(runtime):
    store = incoming(runtime)
    assert not runtime.ingest(message_wire())
    assert len(store.many("SELECT * FROM events")) == 1
    assert len(store.many("SELECT * FROM messages")) == 1
    assert store.one("SELECT can_reply FROM conversations")["can_reply"] == 1


def test_unknown_conversation_cannot_be_messaged(runtime):
    with pytest.raises(ValueError):
        runtime.store.enqueue(CONVERSATION, "hi", "a")


def test_own_message_does_not_authorize_stranger_contact(runtime):
    runtime.ingest(message_wire(sender=100))
    with pytest.raises(ValueError):
        runtime.store.enqueue(CONVERSATION, "hi", "a")


def test_other_account_data_rejected(runtime):
    message = next(messages(message_wire()))
    message["conversation_id"] = "0:1:300:400"
    assert not runtime.store.ingest(message)
    assert not runtime.store.many("SELECT * FROM conversations")


def test_idempotency(runtime):
    store = incoming(runtime)
    first, created = store.enqueue(CONVERSATION, "hello", "key")
    second, created_again = store.enqueue(CONVERSATION, "hello", "key")
    assert created and not created_again
    assert first["id"] == second["id"]
    with pytest.raises(Conflict):
        store.enqueue(CONVERSATION, "different", "key")


def test_server_echo_confirms_send(runtime):
    store = incoming(runtime)
    send, _ = store.enqueue(CONVERSATION, "hello", "key")
    store.set_send_status(send["id"], "submitted")
    runtime.ingest(message_wire(sender=100, msg_id=124, client_id=send["id"]))
    assert store.send(send["id"])["status"] == "confirmed"
    store.set_send_status(send["id"], "unknown")
    assert store.send(send["id"])["status"] == "confirmed"


@pytest.mark.parametrize("status", ["sending", "submitted"])
def test_restart_never_resends_uncertain_messages(runtime, tmp_path, status):
    store = incoming(runtime)
    send, _ = store.enqueue(CONVERSATION, "hello", "key")
    store.set_send_status(send["id"], status)
    other = Store(tmp_path / "private", "100")
    assert other.send(send["id"])["status"] == "unknown"
    other.close()


def test_account_isolation(runtime, tmp_path):
    with pytest.raises(ValueError, match="separate data directory"):
        Store(tmp_path / "private", "300")


def test_allowed_conversations(runtime):
    runtime.allowed = {"0:1:100:300"}
    assert not runtime.ingest(message_wire())
    assert not runtime.store.many("SELECT * FROM messages")
