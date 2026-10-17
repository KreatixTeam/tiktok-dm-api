"""Real HTTP server for the CI image smoke test. Never contacts TikTok."""

import tempfile
from pathlib import Path

import uvicorn

from tiktok_dm_api.app import create_app
from tiktok_dm_api.runtime import Runtime
from tiktok_dm_api.session import Session
from tiktok_dm_api.wire import number

directory = Path(tempfile.mkdtemp())
Session(
    inbox_url="https://im-api.tiktok.com/v2/message/get_by_user_init",
    socket_url="wss://im-ws.tiktok.com/ws/v2",
    cookie="synthetic=ci-fixture",
    user_agent="ci-fixture",
    template=number(1, 203),
    user_id="100",
).save(directory / "session.json")
app = create_app(Runtime(directory), "synthetic-ci-api-token-not-a-secret-0000", run_workers=False)
uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False, log_level="critical")
