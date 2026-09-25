"""Bearer auth in front of the HTTP transport, and the ASGI app around it."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

from mintbot_mcp import __version__
from mintbot_mcp.app import build_http_app
from mintbot_mcp.auth import BearerAuth
from tests.conftest import Rig


def test_an_empty_token_is_refused_at_construction():
    with pytest.raises(ValueError):
        BearerAuth(lambda *_: None, "")


async def test_non_http_scopes_pass_straight_through():
    seen = []

    async def app(scope, _receive, _send):
        seen.append(scope["type"])
    auth = BearerAuth(app, "tok")
    await auth({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


@asynccontextmanager
async def gated_client(rig: Rig):
    """The gated app with the MCP session manager running. ASGITransport does
    not drive lifespan, so the Starlette app's lifespan is entered here - inside
    the test's own task, because anyio cancel scopes must exit where they were
    entered (a pytest-asyncio fixture tears down in another task)."""
    gate = build_http_app(rig.server, "secret-token")
    inner = gate.inner
    async with inner.router.lifespan_context(inner):  # type: ignore[attr-defined]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="http://mcp.test") as client:
            yield client


async def test_health_is_open_and_names_the_server(rig: Rig):
    async with gated_client(rig) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "name": "mintbot-mcp", "version": __version__}


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong"},
    {"Authorization": "Basic c2VjcmV0LXRva2Vu"},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer secret-token-but-longer"},
])
async def test_mcp_endpoint_rejects_missing_or_wrong_tokens(rig: Rig, headers):
    async with gated_client(rig) as client:
        resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["error"] == "unauthorized" and "mintbot-mcp token" in resp.json()["hint"]


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
async def test_mcp_endpoint_accepts_the_token(rig: Rig, scheme):
    async with gated_client(rig) as client:
        resp = await client.get("/mcp", headers={"Authorization": f"{scheme} secret-token"})
    # Past the gate: the MCP transport answers (it refuses a bare GET without a
    # session, but with its own status, never 401).
    assert resp.status_code != 401
