"""Idempotent chat integration: stable identities, no repeated side effects."""
import asyncio

import httpx

from mintbot_mcp.server import build_server
from mintbot_mcp.access import Principal, principal_context
from tests.conftest import sse


def setup_chat(rig):
    rig.local.on('POST', '/mcp/chat/context', {'session_id': 's1', 'project_id': 'main', 'project_name': '', 'workdir': '/home'})
    rig.hermes.on('POST', '/v1/chat/completions', text=sse(['Tere õhtust 🌻']))


async def test_same_key_returns_same_turn_after_server_restart(rig):
    setup_chat(rig)
    first = await rig.call('chat_send', message='hello', idempotency_key='one')
    assert first['status'] == 'done'
    # The registry and server are new; only SQLite survives.
    from mintbot_mcp.chat import TurnRegistry
    rig.backends.turns = TurnRegistry(rig.backends.hermes)
    rig.server = build_server(rig.settings, rig.backends)
    again = await rig.call('chat_send', message='hello', idempotency_key='one')
    assert again['turn_id'] == first['turn_id'] and again['session_id'] == first['session_id']
    assert again['reply'] == first['reply']
    collected = await rig.call('chat_wait', turn_id=first['turn_id'], wait_seconds=0)
    assert collected['reply'] == first['reply'] and collected['status'] == 'done'
    assert (await rig.call('chat_cancel', turn_id=first['turn_id']))['status'] == 'done'
    assert len(rig.hermes.sent('POST', '/v1/chat/completions')) == 1
    conflict = await rig.call('chat_send', message='different', idempotency_key='one')
    assert conflict['code'] == 'idempotency_conflict'


async def test_concurrent_retries_share_one_running_turn(rig):
    setup_chat(rig)
    async def endless():
        yield sse(['working'], done=False).encode()
        await asyncio.Event().wait()
    rig.hermes.handle('POST', '/v1/chat/completions', lambda r: httpx.Response(200, content=endless()))
    replies = await asyncio.gather(*(rig.call('chat_send', message='hello', idempotency_key='key', wait_seconds=0) for _ in range(8)))
    assert len({r['turn_id'] for r in replies}) == 1
    await asyncio.sleep(0.01)
    assert len(rig.hermes.calls) == 1
    await rig.call('chat_cancel', turn_id=replies[0]['turn_id'])


async def test_reservation_precedes_platform_side_effect_and_uncertain_retry_does_not_repeat(rig):
    setup_chat(rig)
    async def interrupted(request):
        raise asyncio.CancelledError()
    rig.local.handle('POST', '/mcp/chat/context', interrupted)
    try:
        await rig.call('chat_send', message='hello', idempotency_key='lost')
    except asyncio.CancelledError:
        pass
    rig.server = build_server(rig.settings, rig.backends)
    reply = await rig.call('chat_send', message='hello', idempotency_key='lost')
    assert reply['code'] == 'outcome_unknown' and reply['status'] == 'unknown'
    assert len(rig.local.calls) == 1 and not rig.hermes.calls


async def test_requested_session_and_project_are_part_of_fingerprint(rig):
    setup_chat(rig)
    first = await rig.call('chat_send', message='hello', idempotency_key='key')
    assert first['ok']
    for params in [{'session_id': 's1'}, {'project_id': 'main'}]:
        result = await rig.call('chat_send', message='hello', idempotency_key='key', **params)
        assert result['code'] == 'idempotency_conflict'


async def test_keys_are_scoped_to_credential_and_replay_checks_current_project(rig):
    setup_chat(rig)
    tools = frozenset({'chat_send'})
    marker = principal_context.set(Principal('alice', 'Alice', False, tools, frozenset({'main'})))
    try:
        first = await rig.call('chat_send', message='hello', idempotency_key='key')
        principal_context.set(Principal('bob', 'Bob', False, tools, frozenset({'main'})))
        second = await rig.call('chat_send', message='hello', idempotency_key='key')
        assert first['turn_id'] != second['turn_id']
        principal_context.set(Principal('alice', 'Alice', False, tools, frozenset({'other'})))
        assert (await rig.call('chat_send', message='hello', idempotency_key='key'))['code'] == 'access_denied'
    finally:
        principal_context.reset(marker)
