# TikTok DM API

A self-hosted, browserless HTTP bridge for TikTok direct messages.

**Unofficial and experimental.** Not affiliated with or endorsed by TikTok.
It uses reverse-engineered endpoints that can change without notice. Use your
own account and respect TikTok's restrictions. This does not unlock restricted
accounts or provide an official Business Messaging API.

## What works

- Existing one-to-one conversations: read messages and send text replies.
- Bearer-authenticated HTTP API and OpenAPI schema.
- SQLite message history, durable send queue and idempotency keys.
- Server-confirmed delivery, with an explicit `unknown` state instead of blind retries.
- Signed, durable webhooks with retries and dead-letter recovery.
- WebSocket reconnects and periodic inbox catch-up.
- One account per instance, with private credential files.
- No permanent browser, external signing service, plugins or cookie-database extraction.

Not implemented: group chats, creating conversations, accepting message requests,
bulk outreach, media downloads/uploads, QR login or automatic session renewal.
Non-text messages are retained as metadata with `content_type: "unsupported"`.

## Quick start with Docker

```sh
git clone https://github.com/KreatixTeam/tiktok-dm-api.git
cd tiktok-dm-api
docker compose build
docker compose run --rm api init
```

### Import your own session

A browser is needed **once for login and explicit session export**, not while the
service runs. Only export an account you own or administer.

1. Sign in to TikTok in desktop Chrome and open your inbox.
2. Open DevTools → **Network**, then refresh the inbox.
3. Find the POST request to
   `https://im-api.tiktok.com/v2/message/get_by_user_init`.
   Right-click → **Copy → Copy as fetch (Node.js)**. Save it as `inbox.fetch`.
4. Switch the Network filter to **WS**. Find the connection to
   `wss://im-ws.tiktok.com/ws/v2` whose query includes `Web-Sdk-Ms-Token`.
   Export it the same way, saving it as `socket.fetch`.
5. Protect the files, then import them. Replace `yourusername` with your own TikTok handle:

```sh
chmod 600 inbox.fetch socket.fetch
python3 scripts/bundle-captures.py inbox.fetch socket.fetch |
  docker compose run --rm -T api import --stdin --username yourusername
docker compose up -d
```

Both files contain **account credentials**. Do not share them, put them in a
repository, paste them into issues or upload them to an online converter.
The importer parses JSON-shaped exports; it never executes the JavaScript.
The user agent is recovered from the exported request metadata when Chrome omits
the header.

If your region uses different hosts or request versions, import fails closed.
Do not work around that by disabling TLS or changing the host allowlist without
reviewing and testing the protocol.

### Call the API

```sh
TOKEN="$(docker compose exec -T api token)"

curl http://127.0.0.1:8000/v1/status \
  -H "Authorization: Bearer $TOKEN"

curl http://127.0.0.1:8000/v1/conversations \
  -H "Authorization: Bearer $TOKEN"
```

Copy a real conversation ID from the second response. For example:

```sh
curl http://127.0.0.1:8000/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-42-reply-1" \
  -d '{"conversation_id":"0:1:123:456","text":"Hello!"}'
```

A new request returns `202` and a send `id`. Poll `/v1/sends/{id}` until its status
is `confirmed`. Repeating the same request with the same idempotency key returns
the original operation, not another TikTok message. Reusing that key for different
text or a different conversation returns `409`.

Only conversations already present in the inbox with an incoming message can be
replied to. This API does not send by arbitrary username.

## Endpoints

All routes except `/healthz` require `Authorization: Bearer <token>`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Process liveness, not TikTok connectivity |
| GET | `/readyz` | Connected transport and a recent successful inbox sync |
| GET | `/v1/status` | Session state and queue status, never session credentials |
| GET | `/v1/conversations` | Conversations, `after` ID and `limit` pagination |
| GET | `/v1/conversations/{id}/messages` | Local history, `after` cursor and `limit` |
| POST | `/v1/messages` | Queue one text reply; `Idempotency-Key` required |
| GET | `/v1/sends/{id}` | Delivery state |
| GET | `/v1/events` | Durable event log with `after` cursor and `limit` |
| POST | `/v1/events/{id}/retry` | Requeue a dead-letter webhook |
| GET | `/openapi.json` | OpenAPI schema with bearer security definitions |

Lists return `items` and `next_cursor`. Persist the cursor when polling. History
contains messages observed by this instance, including the inbox snapshot at first
import; it is not an unlimited historical TikTok export. Initial synchronization
does not emit webhooks for old messages.

Send states: `queued` → `sending` → `submitted` → `confirmed`. Failures can produce
`rejected` or `unknown`. `submitted` only means the frame was written, **not**
that TikTok accepted or delivered it. Confirmation requires a matching server
message. After disconnects, crashes or confirmation timeouts, uncertain sends are
never automatically retried. An `unknown` operation can still become `confirmed`
when its server echo is recovered.

The API accepts at most 4,000 characters per reply, 100 queued sends and 100 items
per page. It attempts at most one outbound send every three seconds.

## Webhooks

Set `TTDM_WEBHOOK_URL` in `.env`, then recreate the container:

```sh
cp .env.example .env
# Edit .env to point to your receiver, preferably over HTTPS.
docker compose up -d --force-recreate
WEBHOOK_SECRET="$(docker compose exec -T api webhook-secret)"
```

Events: `message.received`, `message.sent`, `send.confirmed`.
Chat message events include `message_type`: `7` for text and `8` for media.
Calls and service notifications are ignored, not emitted as incoming messages.
Media remains metadata-only with `content_type: "unsupported"`, even if its
payload contains a caption. Consumers should ignore unknown non-text events and
rate-limit unsupported-media notices rather than replying to every event.
Delivery is at least once. Deduplicate by the stable event `id`.
Non-2xx responses, redirects and timeouts are failures. Delivery retries use
exponential backoff; after 12 failed attempts the event becomes `dead`. Undelivered
events survive restarts. The receiver URL is operator configuration, not an API
parameter, and TikTok credentials are never forwarded to it.

Headers:

```text
X-TTDM-Event-ID: <stable event UUID>
X-TTDM-Signature: t=<Unix seconds>,v1=<hex HMAC-SHA256>
```

The signed bytes are `timestamp + "." + raw_request_body`. Verify the signature
with constant-time comparison and reject timestamps older than five minutes.
Do not parse and reserialize the body before verification.
[A complete receiver example](examples/webhook.py) includes signature verification,
timestamp checks and a durable deduplicated inbox.

## Python integration

[examples/client.py](examples/client.py) provides a small `httpx` client:

```python
from examples.client import TikTokDM

client = TikTokDM("http://127.0.0.1:8000", token)
try:
    operation = client.reply(conversation_id, "Hello!", idempotency_key="my-stable-operation-id")
    delivery = client.send_status(operation["id"])
finally:
    client.close()
```

Persist your operation ID **before** making the request. Reuse it after HTTP
timeouts; do not generate a fresh key for each retry.

## Run without Docker

Python 3.12+ on Linux or macOS:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps .
ttdm init
ttdm import --inbox inbox.fetch --socket socket.fetch --username yourusername
ttdm serve
```

Storage defaults to `./data`. Use `--data /private/path` on each command or set
`TTDM_DATA`. Native serving binds to `127.0.0.1:8000`. `TTDM_API_TOKEN` and
`TTDM_WEBHOOK_SECRET` can override the generated secrets; use at least 32 characters.
`TTDM_ALLOWED_CONVERSATIONS` optionally restricts processing to a comma-separated
set of conversation IDs.

## Resource budget

Compose enforces a **300,000,000-byte memory limit**, disables swap and runs as a
non-root user with a read-only root filesystem. A full three-minute macOS
process-tree measurement, including the sampler, peaked at **93.77 MB** while
performing a real send, confirming it and delivering signed webhook retries.
TikTok's browser was closed throughout. This is a measured workload, not a
guarantee for arbitrary traffic.

Reproduce a native process-tree measurement:

```sh
python scripts/measure-rss.py --seconds 180 -- ttdm serve
```

## Operations and security

- Keep the API on loopback or a private network. For remote use, add HTTPS and
  firewall access; do not expose the generated API token over plain HTTP.
- Keep the data directory at mode `700` and credential files at `600`. It contains
  a TikTok session, API token, webhook secret, private messages and SQLite state.
  Use encrypted host storage and protect backups. Files are not encrypted by this app.
- Use a separate instance and volume for every account. Re-importing another
  account into an existing database is rejected.
- Authentication failure sets `auth_required`; check the account in TikTok.
  Stop the service and repeat the import after a legitimate login or session expiry.
  Do not repeatedly attempt blocked sends or bypass account restrictions.
- Stop the container before importing or backing up its volume. Never delete the
  volume just to restart the service.
- Rotate the API token by replacing its private file while the service is stopped,
  or by supplying a new `TTDM_API_TOKEN` to a native deployment.
- Event history and messages are durable and are not automatically purged. Monitor
  disk use and define retention appropriate to your deployment.

## Development

```sh
pip install -r requirements.lock -e '.[dev]' -c requirements.lock
ruff check src tests examples scripts
ruff format --check src tests examples scripts
mypy src/tiktok_dm_api
pytest -q
pip-audit -r requirements.lock
```

Tests use synthetic fixtures and never contact TikTok. CI also builds and runs the
non-root Docker image with a 300 MB cap. The local signer is vendored with its
upstream MIT license. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
