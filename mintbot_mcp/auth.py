"""Bearer authentication in front of the HTTP transport.

The MCP endpoint is the agent's full control surface, so every request except
the health probe must present the token created at install. Constant-time
compare; a missing or wrong token gets a 401 with ``WWW-Authenticate``.
"""
from __future__ import annotations

import hmac
import json
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class BearerAuth:
    def __init__(self, app: Callable[..., Awaitable[None]], token: str, *, exempt_paths: tuple[str, ...] = ("/health",)) -> None:
        if not token:
            raise ValueError("an empty bearer token would leave the MCP endpoint open")
        self._app = app
        self._token = token.encode("utf-8")
        self._exempt = set(exempt_paths)

    @property
    def inner(self) -> Callable[..., Awaitable[None]]:
        """The application behind the gate (tests drive its lifespan directly)."""
        return self._app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") in self._exempt:
            await self._app(scope, receive, send)
            return
        presented = b""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                parts = value.split(None, 1)
                if len(parts) == 2 and parts[0].lower() == b"bearer":
                    presented = parts[1].strip()
                break
        if presented and hmac.compare_digest(presented, self._token):
            await self._app(scope, receive, send)
            return
        body = json.dumps({
            "error": "unauthorized",
            "hint": "send Authorization: Bearer <token>; the token is printed by `mintbot-mcp token` on the agent VPS",
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        })
        await send({"type": "http.response.body", "body": body})
