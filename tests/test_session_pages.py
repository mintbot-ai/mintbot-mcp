"""Compact and correctly merged project-filtered session pages."""
import httpx

from mintbot_mcp.access import Principal, principal_context


async def test_compact_defaults_drop_large_details_and_include_opt_in(rig):
    row = {'id': 'one', 'title': 'Õun 🍎', 'channel': 'mcp', 'project_id': 'main',
           'project_name': '', 'last_active_at': 42, 'messages': ['huge'], 'model': 'private'}
    rig.local.on('GET', '/mcp/sessions', {'sessions': [row], 'has_more': False})
    reply = await rig.call('chat_sessions')
    assert reply['sessions'] == [{k: row[k] for k in ('id', 'title', 'channel', 'project_id', 'project_name')} | {'last_active': 42}]
    assert reply['has_more'] is False and reply['offset'] == 0
    details = await rig.call('chat_sessions', include_details=True)
    assert details['sessions'][0]['messages'] == ['huge']
    assert not rig.hermes.calls


async def test_restricted_pages_merge_server_filtered_project_windows(rig):
    rows = {'p1': [{'id': 'one', 'last_active_at': 40, 'project_id': 'p1'},
                    {'id': 'three', 'last_active_at': 20, 'project_id': 'p1'}],
            'p2': [{'id': 'two', 'last_active_at': 30, 'project_id': 'p2'},
                    {'id': 'four', 'last_active_at': 10, 'project_id': 'p2'}]}
    def page(request):
        project = request.url.params['project']
        assert project in rows
        limit = int(request.url.params['limit'])
        assert limit == 4
        return httpx.Response(200, json={'sessions': rows[project][:limit], 'has_more': False})
    rig.local.handle('GET', '/mcp/sessions', page)
    marker = principal_context.set(Principal('a', 'A', False, frozenset({'chat_sessions'}), frozenset(rows)))
    try:
        result = await rig.call('chat_sessions', offset=1, limit=2)
        assert [r['id'] for r in result['sessions']] == ['two', 'three']
        assert result['has_more'] and result['next_offset'] == 3
        assert len(rig.local.calls) == 2
        assert (await rig.call('chat_sessions', project_id='hidden'))['code'] == 'access_denied'
        assert (await rig.call('chat_sessions', offset=500))['code'] == 'pagination_limit'
    finally:
        principal_context.reset(marker)
