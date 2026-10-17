import json
from pathlib import Path

import pytest

from tiktok_dm_api.runtime import Runtime
from tiktok_dm_api.session import Session
from tiktok_dm_api.wire import blob, kv, number, string

CONVERSATION = "0:1:100:200"
TOKEN = "unit-test-api-token-not-a-real-credential-0000"


def message_wire(sender=200, msg_id=123, client_id="example-client-id", text="hello"):
    return (
        string(1, CONVERSATION)
        + number(2, 1)
        + number(3, msg_id)
        + number(5, 999)
        + number(7, sender)
        + string(8, json.dumps({"text": text, "aweType": 0}))
        + kv(9, "s:client_message_id", client_id)
        + number(10, 100000)
    )


@pytest.fixture
def runtime(tmp_path: Path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    session = Session(
        inbox_url="https://im-api.tiktok.com/v2/message/get_by_user_init",
        socket_url="wss://im-ws.tiktok.com/ws/v2?test=synthetic",
        cookie="test_cookie=synthetic-test-value",
        user_agent="unit-test",
        template=number(1, 203) + number(2, 10002) + blob(8, blob(203, number(1, 20))),
        user_id="100",
    )
    session.save(directory / "session.json")
    result = Runtime(directory)
    yield result
    result.store.close()
