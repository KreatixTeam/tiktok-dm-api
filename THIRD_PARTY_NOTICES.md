# Third-party code

The local WebSocket signer in `src/tiktok_dm_api/_vendor/bogus.py` is copied from
[Linkmail16/ReLttk-TikTok-Client-Bot](https://github.com/Linkmail16/ReLttk-TikTok-Client-Bot)
at commit `6483b27f441aaf13ebb3ad82b4c49014216a9733`.
The upstream MIT license, copyright 2026 Linkmail, is preserved beside the file.

The text-message protobuf layout and WebSocket framing in `wire.py` are adapted
from that project's `core/proto.py`, under the same MIT license.

No upstream login implementation, browser-cookie database access, plugins,
external media upload service, configuration credentials or disabled-TLS settings
are included.
