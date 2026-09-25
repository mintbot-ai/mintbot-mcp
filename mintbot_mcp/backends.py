"""HTTP clients for the three places a mintbot agent keeps its state.

* :class:`HermesClient` - the stock Hermes API server on loopback (chat turns
  and the session store), authenticated with the agent's ``API_SERVER_KEY``.
* :class:`LocalApiClient` - the panel-local settings daemon on loopback. The
  same key, presented as ``X-Mintbot-Central-Auth``, clears every route that a
  trusted server-to-server caller may use; the browser-only routes additionally
  want a two-factor session cookie when the owner has TOTP on, which
  :meth:`LocalApiClient.unlock` mints from a current code.
* :class:`CentralClient` - the few values that live in the central mintbot
  database (display name, credit, model catalog), reached exactly the way the
  agent's own tools reach them.

Every method returns the backend's JSON; failures raise :class:`BackendError`
so the tool layer can turn them into a structured reply.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .config import Settings

CENTRAL_AUTH_HEADER = "X-Mintbot-Central-Auth"
SESSION_ID_HEADER = "X-Hermes-Session-Id"
SESSION_COOKIE = "mb_sess"
CHAT_MODEL = "hermes-agent"
CHANNEL_MARKER = (
    "[Current channel: MCP client (mintbot-mcp) - the user talks to you through an "
    "external MCP client such as Claude Desktop, Claude Code or Cursor. Reply in plain "
    "Markdown; there is no panel or Telegram UI on this channel, so do not emit panel "
    "markers, MEDIA: attachments or voice replies.]"
)


class BackendError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "error": str(self)}
        if self.status is not None:
            out["status"] = self.status
        if self.code:
            out["code"] = self.code
        return out


class SecondFactorRequired(BackendError):
    def __init__(self) -> None:
        super().__init__(
            "two-factor login is enabled on this agent's panel and this setting is browser-gated; "
            "call security_unlock with a current authenticator code, then retry",
            status=401, code="second_factor_required",
        )


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or resp.reason_phrase or "")[:300]
    if isinstance(body, dict):
        detail = body.get("detail", body.get("error", body))
        return detail if isinstance(detail, str) else json.dumps(detail)[:300]
    return str(body)[:300]


def _json(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        raise BackendError(
            f"{resp.request.method} {resp.request.url.path} failed ({resp.status_code}): {_detail(resp)}",
            status=resp.status_code,
        )
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:2000]}


def _cookie_from(resp: httpx.Response, name: str) -> str | None:
    """Read a Set-Cookie value by hand: the panel marks it Secure, and we talk plain
    HTTP on loopback, so a cookie jar would drop it."""
    for header in resp.headers.get_list("set-cookie"):
        key, _, value = header.split(";", 1)[0].partition("=")
        if key.strip() == name:
            return value.strip() or None
    return None


class HermesClient:
    """The Hermes API server: chat completions with session continuity, session store."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.hermes_url,
            headers={"Authorization": f"Bearer {settings.hermes_api_key}"},
            timeout=httpx.Timeout(30.0, read=None),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> Any:
        return _json(await self._client.get("/health"))

    async def stream_turn(self, message: str, *, session_id: str | None = None) -> AsyncIterator[tuple[str, str]]:
        """Run one agent turn. Yields ``("session", id)`` as soon as Hermes names the
        session, then ``("text", delta)`` for every streamed piece of the reply."""
        headers = {SESSION_ID_HEADER: session_id} if session_id else {}
        body = {
            "model": CHAT_MODEL,
            "stream": True,
            "messages": [
                {"role": "system", "content": CHANNEL_MARKER},
                {"role": "user", "content": message},
            ],
        }
        async with self._client.stream("POST", "/v1/chat/completions", json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise BackendError(f"chat turn refused ({resp.status_code}): {_detail(resp)}", status=resp.status_code)
            assigned = resp.headers.get(SESSION_ID_HEADER)
            if assigned:
                yield ("session", assigned)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or choice.get("message") or {}
                    text = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(text, str) and text:
                        yield ("text", text)

    async def list_sessions(self, *, limit: int = 30, offset: int = 0) -> Any:
        return _json(await self._client.get("/api/sessions", params={"limit": limit, "offset": offset}))

    async def session_messages(self, session_id: str) -> Any:
        return _json(await self._client.get(f"/api/sessions/{session_id}/messages"))

    async def delete_session(self, session_id: str) -> Any:
        return _json(await self._client.delete(f"/api/sessions/{session_id}"))

    async def rename_session(self, session_id: str, title: str) -> Any:
        return _json(await self._client.patch(f"/api/sessions/{session_id}", json={"title": title}))


class LocalApiClient:
    """mintbot-panel-local on loopback, as the trusted server-to-server caller."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.local_api_url,
            headers={CENTRAL_AUTH_HEADER: settings.hermes_api_key},
            timeout=httpx.Timeout(60.0),
            transport=transport,
        )
        self._session_cookie: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def unlocked(self) -> bool:
        return self._session_cookie is not None

    async def request(self, method: str, path: str, *, body: Any = None, params: dict | None = None,
                      timeout: float | None = None) -> Any:
        headers = {"Cookie": f"{SESSION_COOKIE}={self._session_cookie}"} if self._session_cookie else {}
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if body is not None:
            kwargs["json"] = body
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code == 401 and "second_factor_required" in resp.text:
            self._session_cookie = None
            raise SecondFactorRequired()
        return _json(resp)

    async def health(self) -> Any:
        return _json(await self._client.get("/health"))

    async def unlock(self, code: str) -> dict[str, Any]:
        resp = await self._client.post("/auth/totp_verify", json={"code": code})
        if resp.status_code >= 400:
            raise BackendError(f"authenticator code rejected ({resp.status_code}): {_detail(resp)}",
                               status=resp.status_code)
        cookie = _cookie_from(resp, SESSION_COOKIE)
        if not cookie:
            raise BackendError("the panel accepted the code but returned no session cookie")
        self._session_cookie = cookie
        return {"ok": True, "unlocked": True}

    async def lock(self) -> dict[str, Any]:
        if self._session_cookie:
            try:
                await self.request("POST", "/auth/logout")
            finally:
                self._session_cookie = None
        return {"ok": True, "unlocked": False}


class CentralClient:
    """Values that live in the central mintbot database: display name, credit, model catalog."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.central_url or "http://central.unavailable.invalid",
            timeout=httpx.Timeout(15.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _panel(self, path: str) -> str:
        if not self._settings.central_available:
            raise BackendError(
                "the central mintbot API is not configured on this agent "
                "(mintbot.api_base_url / panel_token missing from config.yaml)",
                code="central_unavailable",
            )
        return f"/v1/agent-panel/{self._settings.panel_token}{path}"

    async def get_name(self) -> Any:
        return _json(await self._client.get(self._panel("/name")))

    async def set_name(self, name: str) -> Any:
        return _json(await self._client.post(self._panel("/name"), json={"name": name}))

    async def models(self) -> Any:
        return _json(await self._client.get(self._panel("/models")))

    async def usage(self) -> Any:
        if not self._settings.central_url or self._settings.agent_id is None:
            raise BackendError("the central mintbot API or the agent id is not configured on this agent",
                               code="central_unavailable")
        return _json(await self._client.get(
            f"/internal/agent-usage/{self._settings.agent_id}",
            headers={"Authorization": f"Bearer {self._settings.hermes_api_key}"},
        ))
