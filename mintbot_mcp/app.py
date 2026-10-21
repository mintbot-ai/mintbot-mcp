"""The ASGI application: MCP over streamable HTTP, behind bearer auth, plus /health."""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.server import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .auth import BearerAuth
from .access import AccessDenied, GrantStore, Principal, principal_context
from mcp.shared.exceptions import MCPError
import asyncio
import logging
import secrets

logger = logging.getLogger(__name__)


def _resource_tool(uri: str) -> str | None:
    if uri == 'mintbot://agent':
        return 'settings_overview'
    if uri.startswith('mintbot://sessions/'):
        return 'chat_session_messages'
    return None


class AccessMiddleware:
    """Authorize on actual dispatch; scope comes from each transport request.

    MCP stateful dispatch runs in a session task, so its ambient ContextVar may
    belong to initialize. Never use that ambient identity for later HTTP calls.
    """
    def __init__(self, notifier=None):
        self.notifier = notifier

    async def __call__(self, ctx, call_next):
        scope = getattr(ctx.request, 'scope', None)
        principal = scope.get('mintbot.principal') if scope is not None else None
        if not isinstance(principal, Principal):
            raise MCPError(-32001, 'authentication required')
        marker = principal_context.set(principal)
        try:
            if not principal.owner:
                params = ctx.params or {}
                try:
                    if ctx.method == 'tools/call':
                        principal.require_tool(params.get('name', ''))
                    elif ctx.method == 'resources/read':
                        principal.require_tool(_resource_tool(str(params.get('uri', ''))) or '')
                    elif ctx.method not in {'initialize', 'ping', 'tools/list', 'resources/list',
                                           'resources/templates/list', 'notifications/initialized',
                                           'notifications/cancelled', 'notifications/progress'}:
                        raise AccessDenied('credential does not allow this method')
                except AccessDenied:
                    raise MCPError(-32003, 'credential does not allow this operation') from None
            result = await call_next(ctx)
            if not principal.owner:
                if ctx.method == 'tools/list':
                    result['tools'] = [tool for tool in result.get('tools', [])
                                       if principal.allows_tool(tool['name'])]
                elif ctx.method == 'resources/list':
                    result['resources'] = [r for r in result.get('resources', [])
                                           if principal.allows_tool(_resource_tool(str(r['uri'])) or '')]
                elif ctx.method == 'resources/templates/list':
                    result['resourceTemplates'] = [r for r in result.get('resourceTemplates', [])
                                                   if principal.allows_tool(_resource_tool(r['uriTemplate']) or '')]
            if ctx.method == 'initialize' and scope is not None and self.notifier:
                info = (ctx.params or {}).get('clientInfo') or {}
                # Store no arbitrary client fields, headers, tokens, forwarded IPs or URLs.
                def bounded(value):
                    return ''.join(c for c in str(value)[:128] if c.isprintable())
                notice = {
                    'credential_id': principal.credential_id,
                    'credential_name': principal.name,
                    'connection_id': secrets.token_hex(16),
                    'client_info': {key: bounded(info.get(key, '')) for key in ('name', 'version')},
                    'peer': bounded((scope.get('client') or ('unknown',))[0]),
                    'peer_provenance': 'asgi-client',
                }
                try:
                    await asyncio.wait_for(self.notifier(notice), timeout=3)
                except Exception:
                    logger.warning('MCP connection notice delivery failed')
            return result
        finally:
            principal_context.reset(marker)

MCP_PATH = "/mcp"


def build_http_app(server: MCPServer, token: str, *, grant_store: GrantStore | None = None, notifier=None) -> BearerAuth:
    server.middleware.append(AccessMiddleware(notifier))
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
    return BearerAuth(app, token, grant_store=grant_store)
