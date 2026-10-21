"""Canonical project isolation and compact structured tool replies."""
import json

import httpx

from mintbot_mcp.access import Principal, principal_context
from tests.conftest import sse


async def test_direct_calls_enforce_tool_grants(rig):
    token = principal_context.set(Principal('guest', 'Guest', False))
    try:
        reply = await rig.call('model_get')
        assert reply['code'] == 'access_denied'
        assert not rig.local.calls
    finally:
        principal_context.reset(token)


async def test_structured_reply_has_short_non_json_text(rig):
    rig.local.on('GET', '/config/state', {'model': 'õun 🍎', 'large': 'x' * 10000})
    result = await rig.server.call_tool('model_get', {})
    assert result.structured_content['model'] == 'õun 🍎'
    assert len(result.content[0].text) < 200
    assert 'large' not in result.content[0].text
    rig.local.on('GET', '/config/state', {'status': 'x' * 10000})
    result = await rig.server.call_tool('model_get', {})
    assert len(result.content[0].text) < 200


async def test_new_chat_uses_canonical_project_context(rig):
    rig.local.on('POST', '/mcp/chat/context', {'session_id': 'api:mcp:one', 'project_id': 'p1',
                                           'project_name': 'Õun 🍎', 'workdir': '/work/one'})
    rig.hermes.on('POST', '/v1/chat/completions', text=sse(['Tere õhtust 🌻']))
    reply = await rig.call('chat_send', message='tere', project_id='p1')
    assert reply['project_id'] == 'p1' and reply['project_name'] == 'Õun 🍎'
    assert reply['workdir'] == '/work/one'
    assert reply['session_id'] == 'api:mcp:one'
    assert rig.hermes.calls[-1].headers['x-hermes-session-id'] == 'api:mcp:one'
    assert rig.local.last_json('POST', '/mcp/chat/context') == {'project_id': 'p1'}


async def test_unavailable_platform_never_starts_hermes(rig):
    rig.local.on('POST', '/mcp/chat/context', {'detail': 'runtime unavailable'}, status=503)
    reply = await rig.call('chat_send', message='hello')
    assert reply['ok'] is False
    assert not rig.hermes.calls


async def test_projects_filtered_without_leaking_channel_selection(rig):
    rig.local.on('GET', '/mcp/projects', {'enabled': True, 'runtime_ready': True, 'root': '/private',
        'selected': {'panel': {'id': 'hidden'}}, 'projects': [
            {'id': 'p1', 'name': 'Õun', 'dir': '/one'}, {'id': 'hidden', 'name': 'Secret', 'dir': '/secret'}]})
    token = principal_context.set(Principal('a', 'A', False, frozenset({'projects_list', 'project_get'}), frozenset({'p1'})))
    try:
        reply = await rig.call('projects_list')
        assert [p['id'] for p in reply['projects']] == ['p1']
        assert 'hidden' not in json.dumps(reply) and '/private' not in json.dumps(reply)
        assert (await rig.call('project_get', project_id='p1'))['project']['name'] == 'Õun'
        assert (await rig.call('project_get', project_id='hidden'))['code'] == 'access_denied'
    finally:
        principal_context.reset(token)


async def test_all_session_operations_validate_before_touching_hermes(rig):
    rig.local.on('POST', '/mcp/chat/context', {'detail': 'session_not_found'}, status=404)
    for name, args in [('chat_session_messages', {}), ('chat_session_rename', {'title': 'name'}),
                       ('chat_session_delete', {}), ('chat_send', {'message': 'hello'})]:
        reply = await rig.call(name, session_id='missing', **args)
        assert reply['ok'] is False
    assert not rig.hermes.calls


async def test_session_project_is_checked_for_resources_too(rig):
    rig.local.on('POST', '/mcp/chat/context', {'session_id': 's1', 'project_id': 'hidden', 'project_name': 'Secret', 'workdir': '/secret'})
    token = principal_context.set(Principal('a', 'A', False, frozenset({'chat_session_messages'}), frozenset({'p1'})))
    try:
        result = await rig.server.read_resource('mintbot://sessions/s1')
        assert json.loads(list(result)[0].content)['code'] == 'access_denied'
        assert not rig.hermes.calls
    finally:
        principal_context.reset(token)
