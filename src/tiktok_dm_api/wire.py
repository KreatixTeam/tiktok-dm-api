import json
import re
import time
from collections.abc import Iterator
from typing import Any

from ._vendor.bogus import Signer

MAX_FRAME = 2 * 1024 * 1024
DM_ID = re.compile(r"0:1:(\d{1,20}):(\d{1,20})\Z")


def varint(value: int) -> bytes:
    if not 0 <= value < 2**64:
        raise ValueError("Integer outside protobuf range")
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def number(field: int, value: int) -> bytes:
    return varint(field << 3) + varint(value)


def blob(field: int, value: bytes) -> bytes:
    return varint((field << 3) | 2) + varint(len(value)) + value


def string(field: int, value: str) -> bytes:
    return blob(field, value.encode())


def kv(field: int, key: str, value: str) -> bytes:
    return blob(field, string(1, key) + string(2, value))


def _read(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if pos == len(data):
            raise ValueError("Truncated varint")
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise ValueError("Varint overflow")
        result |= (byte & 127) << shift
        if byte < 128:
            return result, pos
    raise ValueError("Varint overflow")


def fields(data: bytes) -> dict[int, list[tuple[int, Any]]]:
    if len(data) > MAX_FRAME:
        raise ValueError("Frame too large")
    result: dict[int, list[tuple[int, Any]]] = {}
    pos = count = 0
    while pos < len(data):
        count += 1
        if count > 10000:
            raise ValueError("Too many fields")
        tag, pos = _read(data, pos)
        field, wire = tag >> 3, tag & 7
        if not 0 < field < 2**29:
            raise ValueError("Invalid field")
        value: Any
        if wire == 0:
            value, pos = _read(data, pos)
        else:
            if wire == 2:
                size, pos = _read(data, pos)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                raise ValueError("Unsupported wire type")
            if pos + size > len(data):
                raise ValueError("Truncated field")
            value = data[pos : pos + size]
            pos += size
        result.setdefault(field, []).append((wire, value))
    return result


def first(obj: dict, field: int, default: Any = None) -> Any:
    return obj.get(field, [(0, default)])[0][1]


def encode(obj: dict) -> bytes:
    out = bytearray()
    for field, values in obj.items():
        for wire, value in values:
            out.extend(varint((field << 3) | wire))
            if wire == 0:
                out.extend(varint(value))
            elif wire == 2:
                out.extend(varint(len(value)))
                out.extend(value)
            elif wire in (1, 5):
                out.extend(value)
            else:
                raise ValueError("Unsupported wire type")
    return bytes(out)


def walk(data: bytes, depth: int = 0, budget: list[int] | None = None) -> Iterator[dict]:
    budget = budget if budget is not None else [10000]
    if depth > 12 or budget[0] == 0:
        return
    budget[0] -= 1
    try:
        obj = fields(data)
    except ValueError:
        return
    yield obj
    for values in obj.values():
        for wire, value in values:
            if wire == 2 and value and not value.startswith((b"{", b"[")):
                yield from walk(value, depth + 1, budget)


def messages(data: bytes) -> Iterator[dict]:
    for obj in walk(data):
        conv = first(obj, 1)
        if not isinstance(conv, bytes):
            continue
        try:
            conversation = conv.decode()
            content = json.loads(first(obj, 8, b""))
        except (UnicodeError, ValueError, TypeError):
            continue
        if not DM_ID.fullmatch(conversation) or not isinstance(content, dict):
            continue
        msg_id, sender, short_id = first(obj, 3), first(obj, 7), first(obj, 5)
        if not all(isinstance(value, int) and value > 0 for value in (msg_id, sender, short_id)):
            continue
        extensions = {}
        for wire, value in obj.get(9, []):
            if wire != 2:
                continue
            try:
                pair = fields(value)
                extensions[first(pair, 1, b"").decode()] = first(pair, 2, b"").decode()
            except (ValueError, UnicodeError, AttributeError):
                continue
        text = content.get("text")
        supported = isinstance(text, str) and len(text) <= 8000
        client_id = extensions.get("s:client_message_id")
        yield {
            "id": str(msg_id),
            "conversation_id": conversation,
            "short_id": str(short_id),
            "sender_id": str(sender),
            "text": text if supported else None,
            "content_type": "text" if supported else "unsupported",
            "client_id": client_id if client_id and len(client_id) <= 128 else None,
            "timestamp": first(obj, 10, 0),
        }


def send_packet(
    template: bytes, conversation: dict, text: str, client_id: str, index: int
) -> bytes:
    msg = (
        string(1, conversation["id"])
        + number(2, 1)
        + number(3, int(conversation["short_id"]))
        + string(4, json.dumps({"aweType": 0, "text": text}, separators=(",", ":")))
        + kv(5, "s:mentioned_users", "")
        + kv(5, "s:client_message_id", client_id)
        + number(6, 7)
        + string(7, "deprecated")
        + string(8, client_id)
    )
    request = fields(template)
    seq = time.time_ns() // 1000000
    request[1], request[2], request[8] = [(0, 100)], [(0, seq)], [(2, blob(100, msg))]
    prefix = number(1, 10015) + number(2, seq) + number(3, 5) + number(4, 1)
    payload = string(7, "pb") + blob(8, encode(request))
    signature = Signer.sign_ws((prefix + payload).hex()[:32], index)
    headers = b"".join(blob(5, value) for wire, value in request.get(15, []) if wire == 2)
    return prefix + kv(5, "X-Bogus", signature) + headers + payload
