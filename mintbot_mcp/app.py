"""The ASGI application: MCP over streamable HTTP, behind bearer auth, plus /health."""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.server import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .auth import BearerAuth

MCP_PATH = "/mcp"


def build_http_app(server: MCPServer, token: str) -> BearerAuth:
    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "name": "mintbot-mcp", "version": __version__})

    # Host-header (DNS rebinding) checks are off: the bearer token is the gate, and
    # the same app is served on loopback today and behind the agent's nginx vhost
    # tomorrow without a per-host allow-list to maintain.
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerAuth(app, token)
