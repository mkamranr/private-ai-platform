"""CommittingRoute (E11): a request's transaction is committed before its response is sent."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from app.api.routing import SESSION_STATE_KEY, CommittingRoute
from app.config.settings import Settings
from app.main import create_app


class FakeSession:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.pending = True

    def in_transaction(self) -> bool:
        return self.pending

    async def commit(self) -> None:
        if self.fail:
            self.events.append("commit-failed")
            raise RuntimeError("could not serialize access")
        self.events.append("commit")
        self.pending = False


class RecordingJSON(JSONResponse):
    events: list[str]

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.events.append("sent")
        await super().__call__(scope, receive, send)


def _app(events: list[str], *, fail: bool = False) -> FastAPI:
    app = FastAPI()
    router = APIRouter(route_class=CommittingRoute)

    async def session_dep(request: Request) -> AsyncIterator[FakeSession]:
        session = FakeSession(events, fail=fail)
        setattr(request.state, SESSION_STATE_KEY, session)
        yield session
        events.append("dependency-closed")

    @router.post("/write")
    async def write(session: FakeSession = Depends(session_dep)) -> JSONResponse:
        events.append("handler")
        response = RecordingJSON({"ok": True})
        response.events = events
        return response

    @router.get("/stream")
    async def stream(session: FakeSession = Depends(session_dep)) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            events.append("streaming")
            yield b"x"

        return StreamingResponse(body())

    app.include_router(router)
    return app


async def _call(app: FastAPI, method: str, path: str) -> int:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return (await client.request(method, path)).status_code


async def test_commit_happens_before_the_response_is_sent() -> None:
    events: list[str] = []
    assert await _call(_app(events), "POST", "/write") == 200
    assert events == ["handler", "commit", "sent", "dependency-closed"]


async def test_a_failed_commit_is_an_error_not_a_200() -> None:
    events: list[str] = []
    assert await _call(_app(events, fail=True), "POST", "/write") == 500
    assert "sent" not in events


async def test_streaming_responses_commit_when_the_stream_ends() -> None:
    events: list[str] = []
    assert await _call(_app(events), "GET", "/stream") == 200
    assert "commit" not in events
    assert events.index("streaming") < events.index("dependency-closed")


def _api_routes(routes: list[Any]) -> Iterator[APIRoute]:
    """Every APIRoute, including those under included routers (FastAPI nests them)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _api_routes(nested.routes)


def test_every_api_route_commits_before_responding(settings: Settings) -> None:
    app = create_app(settings)
    routes = list(_api_routes(app.routes))
    assert len(routes) > 100
    missing = sorted(
        f"{sorted(r.methods)} {r.path}" for r in routes if not isinstance(r, CommittingRoute)
    )
    assert missing == [], "routers must use route_class=CommittingRoute"
