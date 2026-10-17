import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .wire import MAX_FRAME, fields, first, messages


class TransportError(Exception):
    pass


class AuthenticationError(TransportError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise TransportError("Redirect refused")


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Data directory must be private (chmod 700)")


def private_json(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Session files must be private (chmod 600)")
    return json.loads(path.read_text())


def write_private(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_url(url: str, kind: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    expected = {
        "inbox": ("https", "im-api.tiktok.com", "/v2/message/get_by_user_init"),
        "socket": ("wss", "im-ws.tiktok.com", "/ws/v2"),
    }[kind]
    if (parsed.scheme, parsed.hostname, parsed.path) != expected:
        raise ValueError("Unsupported TikTok destination")
    if parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
        raise ValueError("Invalid destination")


def fetch_export(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Captures contain credentials; use chmod 600 before importing")
    return parse_capture(path.read_text())


def parse_capture(value: str) -> dict:
    if len(value) > MAX_FRAME * 4:
        raise ValueError("Capture too large")
    if value.lstrip().startswith("{"):
        captured = json.loads(value)
    else:
        match = re.fullmatch(
            r'\s*fetch\(("(?:[^"\\]|\\.)*"),\s*(\{.*\})\s*\);?\s*', value, re.DOTALL
        )
        if not match:
            raise ValueError("Expected Chrome Copy as fetch (Node.js), not executable JavaScript")
        captured = {"url": json.loads(match[1]), **json.loads(match[2])}
    if not isinstance(captured, dict):
        raise ValueError("Invalid capture")
    return captured


@dataclass(repr=False)
class Session:
    inbox_url: str = field(repr=False)
    socket_url: str = field(repr=False)
    cookie: str = field(repr=False)
    user_agent: str
    template: bytes = field(repr=False)
    user_id: str = ""

    def __post_init__(self):
        validate_url(self.inbox_url, "inbox")
        validate_url(self.socket_url, "socket")
        if (
            not self.cookie
            or len(self.cookie) > 65536
            or "\r" in self.cookie
            or "\n" in self.cookie
        ):
            raise ValueError("Invalid cookie")
        if (
            not self.user_agent
            or len(self.user_agent) > 1024
            or any(c in self.user_agent for c in "\r\n")
        ):
            raise ValueError("Invalid user agent")
        if self.user_id and not re.fullmatch(r"\d{1,24}", self.user_id):
            raise ValueError("Invalid account ID")
        root = fields(self.template)
        if first(root, 1) != 203:
            raise ValueError("Expected inbox initialization request (command 203)")

    @classmethod
    def load(cls, path: Path):
        obj = private_json(path)
        return cls(**{**obj, "template": base64.b64decode(obj["template"], validate=True)})

    def save(self, path: Path) -> None:
        write_private(
            path,
            {
                "inbox_url": self.inbox_url,
                "socket_url": self.socket_url,
                "cookie": self.cookie,
                "user_agent": self.user_agent,
                "template": base64.b64encode(self.template).decode(),
                "user_id": self.user_id,
            },
        )

    def _request(self, url: str, body: bytes | None = None) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"im-api.tiktok.com", "www.tiktok.com"}
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.path not in {"/v2/message/get_by_user_init", "/tiktok/v1/im/user/profile/"}
        ):
            raise TransportError("Destination refused")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Cookie": self.cookie,
                "User-Agent": self.user_agent,
                "Origin": "https://www.tiktok.com",
                "Referer": "https://www.tiktok.com/",
                "Content-Type": "application/x-protobuf",
                "Accept": "application/x-protobuf" if body is not None else "application/json",
            },
        )
        try:
            with opener.open(request, timeout=20) as response:
                data = response.read(MAX_FRAME + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthenticationError("TikTok rejected the session") from None
            raise TransportError("TikTok inbox request failed") from None
        except Exception:
            raise TransportError("TikTok inbox request failed") from None
        if len(data) > MAX_FRAME:
            raise TransportError("Response exceeds size limit")
        return data

    def inbox(self) -> bytes:
        data = self._request(self.inbox_url, self.template)
        root = fields(data)
        if first(root, 3, 0) != 0:
            raise AuthenticationError(
                "TikTok rejected the session; re-import after checking the account"
            )
        return data

    def identify(self, data: bytes, username: str = "") -> None:
        participants: set[str] | None = None
        for message in messages(data):
            ids = set(message["conversation_id"].split(":")[2:])
            participants = ids if participants is None else participants & ids
        if self.user_id:
            if participants is not None and self.user_id not in participants:
                raise ValueError("Account ID does not match inbox conversations")
        elif username and participants:
            query = urllib.parse.urlencode(
                {"aid": "1988", "user_ids": json.dumps(sorted(participants))}
            )
            profiles = json.loads(
                self._request("https://www.tiktok.com/tiktok/v1/im/user/profile/?" + query)
            )
            for user in profiles.get("users", []):
                profile = user.get("im_user_profile", {})
                identifier = str(profile.get("user_id_str", ""))
                if (
                    profile.get("unique_id", "").lower() == username.lstrip("@").lower()
                    and identifier in participants
                ):
                    self.user_id = identifier
                    return
            raise ValueError("Your username did not match the inbox account")
        elif participants and len(participants) == 1:
            self.user_id = next(iter(participants))
        else:
            raise ValueError(
                "Cannot infer account ID from one conversation; provide --username or --user-id"
            )
