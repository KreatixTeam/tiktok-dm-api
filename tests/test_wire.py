import os

import pytest
from conftest import CONVERSATION, message_wire

from tiktok_dm_api.wire import (
    MAX_FRAME,
    blob,
    encode,
    fields,
    first,
    messages,
    number,
    send_packet,
    string,
    varint,
    walk,
)


@pytest.mark.parametrize("value", [0, 1, 127, 128, 16383, 16384, 2**32, 2**64 - 1])
def test_roundtrip(value):
    data = number(1, value) + string(2, "héllo ❤️")
    assert first(fields(data), 1) == value
    assert encode(fields(data)) == data


@pytest.mark.parametrize(
    "data",
    [
        b"\x00",
        b"\x08\x80",
        b"\x0a\x10short",
        b"\x0f",
        b"\x09x",
        b"\x08" + b"\xff" * 10,
        b"\x08" + b"\xff" * 9 + b"\x02",
        b"\x0a" + varint(MAX_FRAME + 1),
        b"x" * (MAX_FRAME + 1),
    ],
)
def test_rejects_malformed(data):
    with pytest.raises(ValueError):
        fields(data)


@pytest.mark.parametrize("value", [-1, 2**64])
def test_rejects_oversize_integer(value):
    with pytest.raises(ValueError):
        varint(value)


def test_bounded_recursive_parser():
    value = b""
    for _ in range(100):
        value = blob(1, value)
    assert len(list(walk(value))) <= 13
    for _ in range(500):
        list(walk(os.urandom(200)))


def test_extracts_only_messages():
    result = list(messages(blob(8, blob(500, message_wire()))))
    assert len(result) == 1
    assert result[0]["conversation_id"] == CONVERSATION
    assert result[0]["sender_id"] == "200"
    assert result[0]["client_id"] == "example-client-id"
    assert result[0]["message_type"] == 7
    assert not list(messages(number(1, 100) + number(2, 203)))


@pytest.mark.parametrize("message_type", [1, 1800, 1805, 9999])
@pytest.mark.parametrize("text", [None, "Call ended"])
def test_call_and_service_events_are_not_messages(message_type, text):
    assert not list(messages(message_wire(message_type=message_type, text=text)))


def test_media_stays_unsupported_even_if_it_has_a_text_field():
    message = list(messages(message_wire(message_type=8, text="video caption")))[0]
    assert message["message_type"] == 8
    assert message["content_type"] == "unsupported"
    assert message["text"] is None


def test_oversize_text_metadata_only():
    assert list(messages(message_wire(text="x" * 9000)))[0]["content_type"] == "unsupported"
    assert list(messages(message_wire(text="x" * 9000)))[0]["text"] is None


def test_send_uses_real_conversation_short_id(runtime):
    packet = send_packet(
        runtime.session.template,
        {"id": CONVERSATION, "short_id": "999"},
        "hello ❤️",
        "test-client-id",
        1,
    )
    outer = fields(packet)
    request = fields(first(outer, 8))
    body = fields(first(fields(first(request, 8)), 100))
    assert first(request, 1) == 100
    assert first(body, 3) == 999
    assert first(body, 2) == 1
    assert first(body, 8) == b"test-client-id"
    assert first(outer, 3) == 5
