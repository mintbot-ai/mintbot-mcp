"""Machine companion credentials rotate without leaking owner browser authority."""
import dataclasses
import json

import httpx
import pytest

from mintbot_mcp.access import Principal, principal_context
from mintbot_mcp.backends import BackendError, HermesClient, LocalApiClient
from mintbot_mcp.notifications import ConnectionNotifier


async def test_all_hermes_requests_observe_rotation(settings, tmp_path):
    path = tmp_path / 'local_credentials.json'
    path.write_text(json.dumps({'current': {'hermes': 'active'}, 'activated': False}))
    configured = dataclasses.replace(settings, local_credentials_file=path)
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})
    client = HermesClient(configured, transport=httpx.MockTransport(handler))
    try:
        await client.health()
        await client.session_messages('s1')
        path.write_text(json.dumps({'current': {'hermes': 'successor'}, 'activated': False}))
        await client.delete_session('s1')
        await client.rename_session('s1', 'Title')
        await client.list_sessions()
        assert [r.headers['authorization'] for r in seen] == [
            'Bearer active', 'Bearer active', 'Bearer successor', 'Bearer successor', 'Bearer successor']
    finally:
        await client.aclose()


async def test_companion_uses_current_rotating_bearer_and_no_owner_cookie(settings, tmp_path):
    path = tmp_path / 'local_credentials.json'
    path.write_text(json.dumps({'current': {'hermes': 'first'}, 'activated': False}))
    configured = dataclasses.replace(settings, local_credentials_file=path)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True})
    client = LocalApiClient(configured, transport=httpx.MockTransport(handler))
    client._session_cookie = 'owner-cookie'
    try:
        await client.project_request('GET', '/mcp/projects')
        path.write_text(json.dumps({'current': {'hermes': 'first'}, 'activated': True,
                                    'pending': {'hermes': 'second'}}))
        await client.project_request('GET', '/mcp/projects')
        assert [r.headers['authorization'] for r in requests] == ['Bearer first', 'Bearer second']
        assert all('cookie' not in r.headers and 'x-mintbot-central-auth' not in r.headers for r in requests)
        path.write_text('{}')
        with pytest.raises(BackendError):
            await client.project_request('GET', '/mcp/projects')
        assert len(requests) == 2
    finally:
        await client.aclose()


async def test_restricted_request_never_borrows_owner_cookie_or_exposes_unlock(settings):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})
    client = LocalApiClient(settings, transport=httpx.MockTransport(handler))
    client._session_cookie = 'owner-cookie'
    marker = principal_context.set(Principal('grant', 'test', False, frozenset({'model_get'})))
    try:
        assert not client.unlocked
        await client.request('GET', '/config/state')
        assert 'cookie' not in requests[0].headers
    finally:
        principal_context.reset(marker)
        await client.aclose()


async def test_notifier_maps_metadata_to_companion_contract(settings):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True})
    notifier = ConnectionNotifier(settings, transport=httpx.MockTransport(handler))
    await notifier({'credential_id': 'grant', 'client_info': {'name': 'Õun', 'version': '1'},
                    'peer': '127.0.0.1', 'peer_provenance': 'asgi-client'})
    request = requests[0]
    assert request.headers['authorization'] == 'Bearer hermes-key'
    assert json.loads(request.content) == {'credential_id': 'grant', 'client_name': 'Õun',
                                          'client_version': '1', 'peer_address': '127.0.0.1'}
