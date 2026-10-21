import json

from mintbot_mcp.cli import main


def test_access_cli_issues_lists_revokes_without_agent_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('MINTBOT_MCP_STATE_DIR', str(tmp_path / 'state'))
    assert main(['access', 'issue', '--name', 'Auditor', '--ttl', '2h', '--group', 'chat', '--project', 'p1']) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued['token'].startswith('mbg_')
    assert issued['project_ids'] == ['p1']
    assert main(['access', 'list']) == 0
    listing = capsys.readouterr().out
    assert issued['id'] in listing
    assert issued['token'] not in listing
    assert 'secret_hash' not in listing
    assert main(['access', 'revoke', issued['id']]) == 0
    capsys.readouterr()
    assert main(['access', 'list']) == 0
    assert json.loads(capsys.readouterr().out)[0]['revoked'] is True


def test_serve_wires_grants_and_connection_notifications(rig, tmp_path, monkeypatch):
    import mintbot_mcp.app as app_module
    import mintbot_mcp.server as server_module
    import uvicorn
    from mintbot_mcp.cli import _serve

    seen = {}
    original = app_module.build_http_app
    def build(server, token, **kwargs):
        seen.update(kwargs)
        return original(server, token, **kwargs)
    monkeypatch.setattr(app_module, 'build_http_app', build)
    monkeypatch.setattr(server_module, 'make_backends', lambda _: rig.backends)
    monkeypatch.setattr(server_module, 'build_server', lambda *_: rig.server)
    listener = {}
    monkeypatch.setattr(uvicorn, 'run', lambda *_, **kw: listener.update(kw))
    monkeypatch.setenv('MINTBOT_MCP_STATE_DIR', str(tmp_path / 'state'))
    _serve(rig.settings)
    assert seen['grant_store'].path == tmp_path / 'state' / 'access' / 'grants.json'
    assert callable(seen['notifier'])
    assert listener['proxy_headers'] is False
