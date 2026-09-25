"""Fake backends for the tool layer: every HTTP client in mintbot_mcp takes an
httpx transport, so the tests route requests to in-memory handlers and record
what was sent. Nothing here opens a socket."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from mintbot_mcp.config import Settings
from mintbot_mcp.server import Backends, build_server, make_backends


@dataclass
class FakeBackend:
    """A route table (method, path) -> response, plus a log of every request."""

    routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    calls: list[httpx.Request] = field(default_factory=list)

    def on(self, method: str, path: str, body: Any = None, *, status: int = 200,
           text: str | None = None, headers: dict[str, str] | None = None) -> None:
        def respond(_request: httpx.Request) -> httpx.Response:
            if text is not None:
                return httpx.Response(status, text=text, headers=headers)
            return httpx.Response(status, json=body if body is not None else {}, headers=headers)
        self.routes[(method.upper(), path)] = respond

    def handle(self, method: str, path: str, fn: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes[(method.upper(), path)] = fn

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            route = self.routes.get((request.method, request.url.path))
            if route is None:
                return httpx.Response(404, json={"detail": f"no fake route for {request.method} {request.url.path}"})
            result = route(request)
            if hasattr(result, "__await__"):
                result = await result
            return result
        return httpx.MockTransport(handler)

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.calls if r.method == method.upper() and r.url.path == path]

    def last_json(self, method: str, path: str) -> Any:
        return json.loads(self.sent(method, path)[-1].content)


@dataclass
class Rig:
    server: Any
    backends: Backends
    hermes: FakeBackend
    local: FakeBackend
    central: FakeBackend
    settings: Settings

    async def call(self, tool: str, **arguments: Any) -> dict:
        result = await self.server.call_tool(tool, arguments)
        assert result.structured_content is not None, f"{tool} returned no structured content"
        return result.structured_content


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        hermes_url="http://hermes.test", hermes_api_key="hermes-key", local_api_url="http://local.test",
        central_url="https://central.test", panel_token="panel-token", agent_id=1091,
        token_file=tmp_path / "state" / "token",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def rig(settings: Settings):
    hermes, local, central = FakeBackend(), FakeBackend(), FakeBackend()
    backends = make_backends(settings, hermes_transport=hermes.transport(), local_transport=local.transport(),
                             central_transport=central.transport())
    server = build_server(settings, backends)
    try:
        yield Rig(server, backends, hermes, local, central, settings)
    finally:
        await backends.aclose()


def sse(chunks: list[str], *, done: bool = True) -> str:
    """The Hermes chat-completions stream: one SSE event per text delta."""
    events = ["data: " + json.dumps({"choices": [{"delta": {"content": c}}]}) + "\n\n" for c in chunks]
    if done:
        events.append("data: [DONE]\n\n")
    return "".join(events)
