from contextlib import asynccontextmanager
import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from mintbot_mcp.access import GrantStore, current_principal
from mintbot_mcp.app import build_http_app


@asynccontextmanager
async def client_for(tmp_path, *, notifier=None):
    server = MCPServer('access-test')

    @server.tool()
    async def chat_send(message: str = '') -> str:
        return current_principal().credential_id

    @server.tool()
    async def persona_get() -> str:
        return 'private'

    @server.resource('mintbot://agent')
    async def agent() -> str:
        return 'private'

    store = GrantStore(tmp_path / 'access' / 'grants.json')
    grant, secret = store.issue('Guest', tools=['chat_send'], project_ids=['p1'])
    app = build_http_app(server, 'owner-secret', grant_store=store, notifier=notifier)
    async with app.inner.router.lifespan_context(app.inner):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            yield client, store, grant, secret


def rpc(response):
    if response.headers.get('content-type', '').startswith('text/event-stream'):
        return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
    return response.json()


async def post(client, secret, method, params=None, session=None):
    headers = {'Authorization': 'Bearer ' + secret, 'Accept': 'application/json, text/event-stream'}
    if session:
        headers['Mcp-Session-Id'] = session
        headers['MCP-Protocol-Version'] = '2025-03-26'
    return await client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})


async def initialize(client, secret):
    response = await post(client, secret, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                                       'clientInfo': {'name': 'Test', 'version': '1'}})
    assert 'result' in rpc(response), response.text
    return response.headers['mcp-session-id']


async def test_restricted_http_dispatch_list_call_resources_and_revocation(tmp_path):
    async with client_for(tmp_path) as (client, store, grant, secret):
        session = await initialize(client, secret)
        listing = rpc(await post(client, secret, 'tools/list', session=session))
        assert [t['name'] for t in listing['result']['tools']] == ['chat_send']
        allowed = rpc(await post(client, secret, 'tools/call', {'name': 'chat_send'}, session))
        assert grant['id'] in str(allowed)
        denied = rpc(await post(client, secret, 'tools/call', {'name': 'persona_get'}, session))
        assert 'error' in denied
        resources = rpc(await post(client, secret, 'resources/list', session=session))
        assert resources['result']['resources'] == []
        denied = rpc(await post(client, secret, 'resources/read', {'uri': 'mintbot://agent'}, session))
        assert 'error' in denied
        assert (await post(client, 'owner-secret', 'tools/list', session=session)).status_code == 403
        store.revoke(grant['id'])
        assert (await post(client, secret, 'tools/list', session=session)).status_code == 401
    assert current_principal().owner


async def test_successful_initialize_notifies_once_with_bounded_observed_peer(tmp_path):
    notices = []

    async def notify(payload):
        notices.append(payload)

    async with client_for(tmp_path, notifier=notify) as (client, store, grant, secret):
        assert (await post(client, 'invalid', 'initialize')).status_code == 401
        assert notices == []
        session = await initialize(client, secret)
        await post(client, secret, 'tools/list', session=session)
        assert len(notices) == 1
        notice = notices[0]
        assert notice['credential_id'] == grant['id']
        assert notice['credential_name'] == 'Guest'
        assert notice['client_info'] == {'name': 'Test', 'version': '1'}
        assert notice['peer'] == '127.0.0.1'
        assert notice['peer_provenance'] == 'asgi-client'
        assert secret not in json.dumps(notice)
        assert notice['connection_id']


async def test_notice_failure_is_nonfatal_and_secret_free(tmp_path, caplog):
    async def notify(payload):
        raise RuntimeError('owner-secret or sensitive URL')

    async with client_for(tmp_path, notifier=notify) as (client, store, grant, secret):
        await initialize(client, secret)
    assert 'owner-secret' not in caplog.text
    assert 'sensitive URL' not in caplog.text


async def test_dispatch_without_http_scope_cannot_inherit_owner_identity():
    from types import SimpleNamespace
    from mintbot_mcp.app import AccessMiddleware
    from mcp.shared.exceptions import MCPError

    async def next_handler(ctx):
        raise AssertionError('unauthenticated dispatch reached handler')
    with pytest.raises(MCPError, match='authentication required'):
        await AccessMiddleware()(SimpleNamespace(request=None, method='tools/list', params={}), next_handler)


async def test_expiration_is_checked_on_initialized_session_every_request(tmp_path, monkeypatch):
    from mintbot_mcp import access
    async with client_for(tmp_path) as (client, store, grant, secret):
        session = await initialize(client, secret)
        monkeypatch.setattr(access.time, 'time', lambda: grant['expires_at'])
        assert (await post(client, secret, 'tools/list', session=session)).status_code == 401


async def test_request_principals_are_isolated_between_sessions(tmp_path):
    import asyncio
    async with client_for(tmp_path) as (client, store, grant, secret):
        second, second_secret = store.issue('Other', tools=['chat_send'], project_ids=['p1'])
        first_session = await initialize(client, secret)
        second_session = await initialize(client, second_secret)
        async def call(secret, session):
            return rpc(await post(client, secret, 'tools/call', {'name': 'chat_send'}, session))
        first, other = await asyncio.gather(call(secret, first_session), call(second_secret, second_session))
        assert grant['id'] in str(first) and second['id'] not in str(first)
        assert second['id'] in str(other) and grant['id'] not in str(other)


@pytest.mark.parametrize('duplicate', ['Authorization', 'Mcp-Session-Id'])
async def test_duplicate_security_headers_are_rejected(tmp_path, duplicate):
    async with client_for(tmp_path) as (client, store, grant, secret):
        session = await initialize(client, secret)
        headers = [('Authorization', 'Bearer ' + secret), ('Mcp-Session-Id', session),
                   ('Accept', 'application/json, text/event-stream'), ('MCP-Protocol-Version', '2025-03-26')]
        value = 'Bearer ' + secret if duplicate == 'Authorization' else session
        headers.append((duplicate, value))
        response = await client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        assert response.status_code == 401


async def test_owner_compatibility_and_restricted_unknown_methods(tmp_path):
    async with client_for(tmp_path) as (client, store, grant, secret):
        owner_session = await initialize(client, 'owner-secret')
        listing = rpc(await post(client, 'owner-secret', 'tools/list', session=owner_session))
        assert {t['name'] for t in listing['result']['tools']} == {'chat_send', 'persona_get'}
        private = rpc(await post(client, 'owner-secret', 'resources/read', {'uri': 'mintbot://agent'}, owner_session))
        assert 'private' in str(private)
        guest_session = await initialize(client, secret)
        denied = rpc(await post(client, secret, 'prompts/list', session=guest_session))
        assert denied['error']['code'] == -32003
        denied = rpc(await post(client, secret, 'resources/subscribe', {'uri': 'mintbot://agent'}, guest_session))
        assert denied['error']['code'] == -32003
        assert (await post(client, secret, 'tools/list', session=owner_session)).status_code == 403


async def test_notices_bound_client_claims_ignore_forwarded_headers_and_skip_invalid_initialize(tmp_path):
    notices = []
    async def notify(payload):
        notices.append(payload)
    async with client_for(tmp_path, notifier=notify) as (client, store, grant, secret):
        malformed = await post(client, secret, 'initialize', {'protocolVersion': '2025-03-26'})
        assert 'error' in rpc(malformed)
        assert notices == []
        client.headers['X-Forwarded-For'] = '203.0.113.1'
        response = await post(client, secret, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                'clientInfo': {'name': 'x' * 300, 'version': 'a\nb', 'websiteUrl': 'https://example.invalid/secret'}})
        assert 'result' in rpc(response)
        assert len(notices) == 1
        assert notices[0]['client_info'] == {'name': 'x' * 128, 'version': 'ab'}
        assert notices[0]['peer'] == '127.0.0.1'
        assert 'websiteUrl' not in json.dumps(notices[0])
