"""Turn visibility follows the grant as well as canonical project membership."""
import asyncio

import httpx

from mintbot_mcp.access import Principal, principal_context
from tests.conftest import sse


async def test_turns_cannot_be_collected_or_cancelled_by_another_credential(rig):
    rig.local.on('POST', '/mcp/chat/context', {'session_id': 's1', 'project_id': 'p1', 'project_name': 'One', 'workdir': '/one'})
    async def slow():
        yield sse(['hello'], done=False).encode()
        await asyncio.Event().wait()
    rig.hermes.handle('POST', '/v1/chat/completions', lambda r: httpx.Response(200, content=slow()))
    tools = frozenset({'chat_send', 'chat_wait', 'chat_cancel'})
    marker = principal_context.set(Principal('alice', 'Alice', False, tools, frozenset({'p1'})))
    try:
        turn = await rig.call('chat_send', message='hello', project_id='p1', wait_seconds=0)
        principal_context.set(Principal('bob', 'Bob', False, tools, frozenset({'p1'})))
        for name in ['chat_wait', 'chat_cancel']:
            result = await rig.call(name, turn_id=turn['turn_id'], **({'wait_seconds': 0} if name == 'chat_wait' else {}))
            assert result['code'] == 'access_denied'
        assert rig.backends.turns.get(turn['turn_id']).running
        principal_context.set(Principal('alice', 'Alice', False, tools, frozenset({'p1'})))
        rig.local.on('POST', '/mcp/chat/context', {'session_id': 's1', 'project_id': 'hidden'})
        assert (await rig.call('chat_wait', turn_id=turn['turn_id'], wait_seconds=0))['code'] == 'access_denied'
    finally:
        principal_context.reset(marker)
        for t in rig.backends.turns._turns.values():
            if t.task:
                t.task.cancel()
