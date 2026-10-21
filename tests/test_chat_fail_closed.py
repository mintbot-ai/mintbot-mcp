"""Fail closed for unknown existing IDs and expired grant snapshots."""
import time

from mintbot_mcp.access import Principal, principal_context
from tests.conftest import sse


async def test_unknown_explicit_session_cannot_create_a_new_agent_conversation(rig):
    # The platform can bind a legacy ID to main without checking Hermes existence.
    rig.hermes.on('GET', '/api/sessions/missing/messages', {'detail': 'not found'}, status=404)
    rig.hermes.on('POST', '/v1/chat/completions', text=sse(['must not run']))
    reply = await rig.call('chat_send', message='hello', session_id='missing')
    assert reply['ok'] is False
    assert not rig.hermes.sent('POST', '/v1/chat/completions')


async def test_expired_direct_principal_cannot_start_or_read(rig):
    marker = principal_context.set(Principal('alice', 'Alice', False, frozenset({'projects_list'}), None, time.time() - 1))
    try:
        assert (await rig.call('projects_list'))['code'] == 'access_denied'
        assert not rig.local.calls
    finally:
        principal_context.reset(marker)


async def test_explicit_empty_or_padded_identifiers_are_not_silently_repaired(rig):
    for params in [{'session_id': ''}, {'session_id': ' padded '}, {'session_id': 'one\n'},
                   {'project_id': ''}, {'project_id': ' main '}]:
        result = await rig.call('chat_send', message='hello', **params)
        assert result['code'] == 'invalid_argument'
    assert not rig.local.calls and not rig.hermes.calls


async def test_platform_cannot_silently_redirect_a_new_main_session(rig):
    rig.local.on('POST', '/mcp/chat/context', {'session_id': 'new', 'project_id': 'other', 'workdir': '/other'})
    result = await rig.call('chat_send', message='hello')
    assert result['code'] == 'invalid_context'
    assert not rig.hermes.calls


async def test_idempotency_storage_failure_is_structured_and_never_starts_backends(rig, monkeypatch):
    import sqlite3
    from mintbot_mcp import idempotency
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('private database unavailable')
    monkeypatch.setattr(idempotency.sqlite3, 'connect', unavailable)
    result = await rig.call('chat_send', message='hello', idempotency_key='key')
    assert result['code'] == 'idempotency_unavailable'
    assert not rig.local.calls and not rig.hermes.calls
