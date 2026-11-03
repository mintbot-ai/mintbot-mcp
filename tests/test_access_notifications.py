import httpx


async def test_notice_client_posts_to_local_companion_with_server_auth(rig):
    from mintbot_mcp.notifications import ConnectionNotifier

    seen = []
    async def handle(request):
        seen.append(request)
        return httpx.Response(200, json={'ok': True})

    notifier = ConnectionNotifier(rig.settings, transport=httpx.MockTransport(handle))
    payload = {'credential_id': 'synthetic', 'connection_id': 'abc', 'client_info': {'name': 'Test'}}
    await notifier(payload)
    assert seen[0].url.path == '/mcp/connections'
    assert seen[0].headers['Authorization'] == f'Bearer {rig.settings.hermes_api_key}'
    assert __import__('json').loads(seen[0].content) == {
        'credential_id': 'synthetic', 'credential_name': '', 'owner': False,
        'client_name': 'Test', 'client_version': '', 'peer_address': ''}


async def test_notice_names_the_credential_and_whether_it_is_the_owners(rig):
    """The owner reads who connected: the owner token or a limited grant by
    its name, not a credential fingerprint."""
    from mintbot_mcp.notifications import ConnectionNotifier
    import json

    seen = []
    async def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'ok': True})

    notifier = ConnectionNotifier(rig.settings, transport=httpx.MockTransport(handle))
    await notifier({'credential_id': 'owner', 'credential_name': 'Owner', 'owner': True,
                    'client_info': {'name': 'Claude Desktop', 'version': '1.2'}, 'peer': '127.0.0.1'})
    await notifier({'credential_id': 'a1b2', 'credential_name': 'Guest ' + 'x' * 100, 'owner': False,
                    'client_info': {}, 'peer': ''})
    assert seen[0]['owner'] is True and seen[0]['credential_name'] == 'Owner'
    assert seen[0]['client_name'] == 'Claude Desktop' and seen[0]['client_version'] == '1.2'
    assert seen[1]['owner'] is False and seen[1]['credential_name'] == ('Guest ' + 'x' * 100)[:80]
    assert seen[1]['client_name'] == 'Unknown MCP client'
