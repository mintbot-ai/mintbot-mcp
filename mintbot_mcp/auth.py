"""Bearer authentication and credential/session binding for HTTP transport.

The original owner token remains compatible. Restricted grants are reread from
private durable state on every request, including initialized sessions. Neither
this gate nor connection notices consume or buffer request/response streams.
"""
from __future__ import annotations

import hmac
import json
import hashlib

from .access import GrantStore, Principal, principal_context
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class BearerAuth:
    def __init__(self, app: Callable[..., Awaitable[None]], token: str, *, exempt_paths: tuple[str, ...] = ("/health",), grant_store: GrantStore | None = None) -> None:
        if not token:
            raise ValueError("an empty bearer token would leave the MCP endpoint open")
        self._app = app
        self._token = token.encode("utf-8")
        self._exempt = set(exempt_paths)
        self._store = grant_store
        self._sessions: dict[bytes, tuple[str, str]] = {}

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
        principal = None
        security_headers = [name.lower() for name, _ in scope.get('headers') or []]
        ambiguous = any(security_headers.count(name) > 1
                        for name in (b'authorization', b'mcp-session-id'))
        if ambiguous:
            presented = b''
        if presented and hmac.compare_digest(presented, self._token):
            principal = Principal()
        elif presented and self._store:
            try:
                principal = self._store.authenticate(presented.decode('utf-8'))
            except (ValueError, OSError, KeyError, TypeError):
                principal = None  # Corrupt/unreadable state fails closed.
        if principal is not None:
            # Bind a session to both credential ID and token fingerprint (owner rotation).
            binding = (principal.credential_id, hashlib.sha256(presented).hexdigest())
            headers = dict(scope.get('headers') or [])
            session = headers.get(b'mcp-session-id')
            if session and self._sessions.get(session) != binding:
                await send({'type': 'http.response.start', 'status': 403,
                            'headers': [(b'content-type', b'application/json')]})
                await send({'type': 'http.response.body', 'body': b'{"error":"session credential mismatch"}'})
                return
            scope['mintbot.principal'] = principal

            async def bound_send(message):
                if message['type'] == 'http.response.start':
                    for key, value in message.get('headers', []):
                        if key.lower() == b'mcp-session-id' and message['status'] < 400:
                            self._sessions[value] = binding
                await send(message)

            marker = principal_context.set(principal)
            try:
                await self._app(scope, receive, bound_send)
            finally:
                principal_context.reset(marker)
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
