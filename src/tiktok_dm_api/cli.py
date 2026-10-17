import argparse
import fcntl
import json
import os
import secrets
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .runtime import Runtime
from .session import Session, TransportError, fetch_export, parse_capture, secure_directory
from .wire import MAX_FRAME, fields, first


def secret_file(path: Path) -> str:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Secret files must be private (chmod 600)")
    return path.read_text().strip()


def init(directory: Path):
    secure_directory(directory)
    for name in ("api-token", "webhook-secret"):
        path = directory / name
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_urlsafe(32) + "\n")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Unofficial self-hosted TikTok DM bridge")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "import", "serve", "token", "webhook-secret"):
        sub = subs.add_parser(name)
        sub.add_argument("--data", type=Path, default=Path(os.getenv("TTDM_DATA", "./data")))
        if name == "import":
            sub.add_argument("--inbox", type=Path)
            sub.add_argument("--socket", type=Path)
            sub.add_argument(
                "--stdin", action="store_true", help="Read a JSON capture bundle from stdin"
            )
            sub.add_argument("--user-id", default="")
            sub.add_argument("--username", default="", help="Your own TikTok @username")
            sub.add_argument("--user-agent", default="", help="Override the captured user agent")
        elif name == "serve":
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        init(args.data)
        if args.command == "init":
            print("Initialized private storage. Import a session before starting.")
            return
        if args.command in {"token", "webhook-secret"}:
            print(
                secret_file(
                    args.data / ("api-token" if args.command == "token" else "webhook-secret")
                )
            )
            return
        with (args.data / "daemon.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "Stop the service before importing or starting another instance"
                ) from None
            if args.command == "import":
                if args.stdin:
                    bundle = json.loads(sys.stdin.read(MAX_FRAME * 8 + 1))
                    inbox, socket = bundle["inbox"], bundle["socket"]
                    inbox = parse_capture(inbox) if isinstance(inbox, str) else inbox
                    socket = parse_capture(socket) if isinstance(socket, str) else socket
                elif args.inbox and args.socket:
                    inbox, socket = fetch_export(args.inbox), fetch_export(args.socket)
                else:
                    raise ValueError("Provide --inbox and --socket, or --stdin")
                headers = {k.lower(): v for k, v in inbox["headers"].items()}
                template = inbox["body"].encode("latin1")
                metadata = {
                    first(fields(value), 1, b"").decode(): first(fields(value), 2, b"").decode()
                    for wire, value in fields(template).get(15, [])
                    if wire == 2
                }
                session = Session(
                    inbox_url=inbox["url"],
                    socket_url=socket["url"],
                    cookie=headers.get("cookie", ""),
                    user_agent=args.user_agent
                    or headers.get("user-agent")
                    or metadata.get("user_agent", ""),
                    template=template,
                    user_id=args.user_id,
                )
                session.identify(session.inbox(), username=args.username)
                # Reject account changes before replacing a working session.
                from .store import Store

                store = Store(args.data, session.user_id)
                store.close()
                session.save(args.data / "session.json")
                print(
                    "Session verified and imported. No browser is needed for the running service."
                )
                return
            allowed = os.getenv("TTDM_ALLOWED_CONVERSATIONS")
            runtime = Runtime(
                args.data,
                webhook_url=os.getenv("TTDM_WEBHOOK_URL", ""),
                webhook_secret=os.getenv("TTDM_WEBHOOK_SECRET")
                or secret_file(args.data / "webhook-secret"),
                allowed_conversations=set(allowed.split(",")) if allowed else None,
            )
            token = os.getenv("TTDM_API_TOKEN") or secret_file(args.data / "api-token")
            uvicorn.run(
                create_app(runtime, token),
                host=args.host,
                port=args.port,
                access_log=False,
                log_level="critical",
                limit_concurrency=16,
                timeout_keep_alive=5,
                h11_max_incomplete_event_size=16384,
                proxy_headers=False,
            )
    except (ValueError, KeyError, OSError, TypeError, TransportError):
        parser.exit(
            1,
            "Configuration or import failed. Check private-file permissions, capture format, account ID and session validity.\n",
        )


if __name__ == "__main__":
    main()
