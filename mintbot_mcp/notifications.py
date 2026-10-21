"""Private connection notices to the panel-local companion.

Deduplication and owner delivery belong to the platform endpoint. Do not log
HTTP errors: request URLs, response bodies and credentials can contain secrets.
"""
from __future__ import annotations

import httpx

from .config import Settings


class ConnectionNotifier:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def __call__(self, payload: dict) -> None:
        # One bounded call; no persistent client or cookie jar to share with TOTP.
        async with httpx.AsyncClient(base_url=self.settings.local_api_url, timeout=2,
                                     transport=self.transport, follow_redirects=False,
                                     headers={'X-Mintbot-Central-Auth': self.settings.hermes_api_key}) as client:
            response = await client.post('/mcp/connections', json=payload)
            response.raise_for_status()
