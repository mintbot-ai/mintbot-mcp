"""Private connection notices to the panel-local companion.

Deduplication and owner delivery belong to the platform endpoint. Do not log
HTTP errors: request URLs, response bodies and credentials can contain secrets.
"""
from __future__ import annotations

import httpx

from .config import Settings
from .backends import LocalApiClient


class ConnectionNotifier:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def __call__(self, payload: dict) -> None:
        info = payload.get('client_info') or {}
        body = {'credential_id': payload['credential_id'],
                'client_name': info.get('name') or 'Unknown MCP client',
                'client_version': str(info.get('version') or '')[:80],
                'peer_address': str(payload.get('peer') or '')[:64]}
        client = LocalApiClient(self.settings, transport=self.transport)
        try:
            await client.project_request('POST', '/mcp/connections', body=body, timeout=2)
        finally:
            await client.aclose()
