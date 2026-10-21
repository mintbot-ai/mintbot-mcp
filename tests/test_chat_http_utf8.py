"""Exercise real MCP streamable HTTP JSON/SSE serialization, not tool dicts."""
import json
import asyncio
import socket

import httpx
import pytest
import uvicorn

from mintbot_mcp.app import build_http_app
from tests.test_access_http import initialize, post, rpc


@pytest.mark.parametrize('json_response', [False, True])
async def test_utf8_roundtrip_over_streamable_http(rig, monkeypatch, json_response):
    reply = 'Tere õhtust! Täna söön õunu, öösel käin saunas. 🧖‍♀️🍎'
    async def stream():
        event = ('data: ' + json.dumps({'choices': [{'delta': {'content': reply}}]}, ensure_ascii=False) + '\n\ndata: [DONE]\n\n').encode('utf-8')
        # Split every byte: the transport must decode multibyte code points incrementally.
        for byte in event:
            yield bytes([byte])
    rig.hermes.handle('POST', '/v1/chat/completions', lambda req: httpx.Response(200,
        content=stream(), headers={'Content-Type': 'text/event-stream'}))
    original = rig.server.streamable_http_app
    monkeypatch.setattr(rig.server, 'streamable_http_app', lambda **kwargs: original(json_response=json_response, **kwargs))
    app = build_http_app(rig.server, 'owner-secret')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', proxy_headers=False, ws='none'))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError('HTTP server stopped before readiness')
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
            session = await initialize(client, 'owner-secret')
            response = await post(client, 'owner-secret', 'tools/call', {
                'name': 'chat_send', 'arguments': {'message': 'Õunad, öö ja päike ☀️'}}, session)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
    assert response.status_code == 200
    raw = response.content.decode('utf-8', errors='strict')
    assert '\ufffd' not in raw and 'Ã' not in raw
    assert reply in raw  # UTF-8 bytes on the wire, not merely ASCII JSON escapes.
    assert ('application/json' if json_response else 'text/event-stream') in response.headers['content-type']
    if not json_response:
        assert 'charset=utf-8' in response.headers['content-type'].lower()
    result = rpc(response)['result']
    assert result['structuredContent']['reply'] == reply
    assert result['structuredContent']['project_id'] == 'main'
    assert result['content'][0]['text'] == 'chat_send: done. Full result is in structuredContent.'
    assert reply not in result['content'][0]['text']
    assert rig.hermes.last_json('POST', '/v1/chat/completions')['messages'][-1]['content'] == 'Õunad, öö ja päike ☀️'
