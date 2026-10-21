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
    assert seen[0].headers['X-Mintbot-Central-Auth'] == rig.settings.hermes_api_key
    assert seen[0].content == __import__('json').dumps(payload, separators=(',', ':')).encode()
