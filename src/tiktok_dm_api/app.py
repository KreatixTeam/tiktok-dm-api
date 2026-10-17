import hmac
import json
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .runtime import Runtime
from .store import Conflict

bearer = HTTPBearer(auto_error=False)


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^0:1:\d+:\d+$", max_length=100)
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def nonempty(cls, value: str):
        if not value.strip() or "\x00" in value:
            raise ValueError("Text must not be empty or contain NUL")
        return value


def create_app(runtime: Runtime, api_token: str, run_workers: bool = True) -> FastAPI:
    if len(api_token) < 32 or not api_token.isascii():
        raise ValueError("API token must contain at least 32 ASCII characters")

    async def authenticated(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    ):
        if not credentials or not hmac.compare_digest(
            credentials.credentials.encode(), api_token.encode()
        ):
            raise HTTPException(401, "Invalid API token", headers={"WWW-Authenticate": "Bearer"})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_workers:
            await runtime.start()
        try:
            yield
        finally:
            if run_workers:
                await runtime.stop()

    app = FastAPI(
        title="TikTok DM API",
        version=__version__,
        lifespan=lifespan,
        description="Unofficial self-hosted bridge. Not affiliated with or endorsed by TikTok.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    auth = [Depends(authenticated)]

    @app.middleware("http")
    async def limits(request: Request, call_next):
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 32768:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors may echo request values. Keep credentials and message text out.
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse({"error": "internal_error"}, status_code=500)

    @app.get("/healthz", tags=["Health"])
    async def health():
        return {"status": "ok"}

    @app.get("/v1/status", dependencies=auth, tags=["Session"])
    async def status():
        return runtime.status()

    @app.get("/readyz", dependencies=auth, tags=["Health"])
    async def ready():
        import time

        healthy = (
            runtime.state == "connected"
            and runtime.last_sync is not None
            and (time.time() - runtime.last_sync < 60)
        )
        return JSONResponse({"ready": healthy}, status_code=200 if healthy else 503)

    @app.get("/openapi.json", dependencies=auth, include_in_schema=False)
    async def openapi():
        return app.openapi()

    @app.get("/v1/conversations", dependencies=auth, tags=["Conversations"])
    async def conversations(
        after: str = Query("", max_length=100), limit: int = Query(50, ge=1, le=100)
    ):
        items = runtime.store.many(
            "SELECT * FROM conversations WHERE id>? ORDER BY id LIMIT ?", (after, limit)
        )
        return {"items": items, "next_cursor": items[-1]["id"] if items else after}

    @app.get("/v1/conversations/{conversation_id}/messages", dependencies=auth, tags=["Messages"])
    async def history(
        conversation_id: str, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
    ):
        items = runtime.store.many(
            """SELECT row_id AS cursor,id,conversation_id,sender_id,text,content_type,client_id,timestamp
            FROM messages WHERE conversation_id=? AND row_id>? ORDER BY row_id LIMIT ?""",
            (conversation_id, after, limit),
        )
        return {"items": items, "next_cursor": items[-1]["cursor"] if items else after}

    @app.post("/v1/messages", dependencies=auth, status_code=202, tags=["Messages"])
    async def send(
        payload: SendRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128, pattern=r"^[!-~]+$")],
    ):
        if runtime.allowed is not None and payload.conversation_id not in runtime.allowed:
            raise HTTPException(403, "Conversation not permitted")
        try:
            result, created = runtime.store.enqueue(
                payload.conversation_id, payload.text, idempotency_key
            )
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from None
        return JSONResponse(result, status_code=202 if created else 200)

    @app.get("/v1/sends/{send_id}", dependencies=auth, tags=["Messages"])
    async def send_status(send_id: str):
        result = runtime.store.send(send_id)
        if result is None:
            raise HTTPException(404, "Send not found")
        return result

    @app.get("/v1/events", dependencies=auth, tags=["Events"])
    async def events(after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        items = runtime.store.many(
            """SELECT row_id AS cursor,id,type,data,status,attempts,created_at
            FROM events WHERE row_id>? ORDER BY row_id LIMIT ?""",
            (after, limit),
        )
        for item in items:
            item["data"] = json.loads(item["data"])
        return {"items": items, "next_cursor": items[-1]["cursor"] if items else after}

    @app.post("/v1/events/{event_id}/retry", dependencies=auth, tags=["Events"])
    async def retry_event(event_id: str):
        with runtime.store.db:
            result = runtime.store.db.execute(
                "UPDATE events SET status='pending',attempts=0,next_at=0 WHERE id=? AND status='dead'",
                (event_id,),
            )
            if result.rowcount != 1:
                raise HTTPException(404, "Dead-letter event not found")
        return {"status": "pending"}

    return app
