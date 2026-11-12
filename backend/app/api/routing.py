"""Commit a request's transaction before its response is sent (E11).

FastAPI (0.118 and later) closes a ``yield`` dependency with the default "request" scope
*after* the response has been sent. ``session_scope`` commits in that exit code, so on every
write:

* the client had its 200 before the data was committed, and a page that reloads straight
  away could read the old state (the race E8 worked around with explicit commits);
* a commit that then failed rolled the change back after the client was told it succeeded.

``CommittingRoute`` commits the request's session as soon as the endpoint has returned its
response object, before a byte is sent. A failed commit therefore becomes an error response,
and ``session_scope`` still rolls back, as it does for any exception.

**Streaming responses are left alone.** Their body runs after the endpoint returns, and
some (an agent run streamed over SSE) persist as they go on the request's session. They
keep the old behaviour: the commit happens when the stream ends.

Every router in ``app.api`` must use this class. ``tests/unit/test_routing.py`` fails if a
route does not.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

#: Where ``get_session`` leaves the request's session for the route to find.
SESSION_STATE_KEY = "db_session"


class CommittingRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def commit_then_respond(request: Request) -> Response:
            response = await handler(request)
            if not isinstance(response, StreamingResponse):
                session: AsyncSession | None = getattr(request.state, SESSION_STATE_KEY, None)
                if session is not None and session.in_transaction():
                    await session.commit()
            return response

        return commit_then_respond
