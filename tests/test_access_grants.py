import json
import os

import pytest


def test_grant_issue_hash_permissions_expiry_revoke(tmp_path):
    from mintbot_mcp.access import GrantStore

    store = GrantStore(tmp_path / 'private' / 'grants.json')
    grant, secret = store.issue('Guest', tools=['chat_send'], project_ids=['p1'], ttl=60, now=100)
    principal = store.authenticate(secret, now=101)
    assert principal.credential_id == grant['id']
    assert principal.allows_tool('chat_send')
    assert not principal.allows_tool('persona_set')
    principal.require_project('p1')
    with pytest.raises(PermissionError):
        principal.require_project('p2')
    raw = store.path.read_text()
    assert secret not in raw
    assert json.loads(raw)['grants'][0]['secret_hash']
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert os.stat(store.path.parent).st_mode & 0o777 == 0o700
    assert store.authenticate(secret, now=160) is None
    store.revoke(grant['id'])
    assert GrantStore(store.path).authenticate(secret, now=102) is None


def test_chat_group_uses_real_registered_session_tool(tmp_path):
    from mintbot_mcp.access import GrantStore
    store = GrantStore(tmp_path / 'private' / 'grants.json')
    grant, secret = store.issue('Guest', groups=['chat'], project_ids=['p1'])
    principal = store.authenticate(secret)
    assert principal.allows_tool('chat_sessions')
    assert 'chat_sessions_list' not in grant['tools']


@pytest.mark.parametrize('tool', ['security_state', 'persona_set', 'telegram_bot_apply', 'extensions_install'])
def test_owner_control_tools_cannot_be_delegated(tmp_path, tool):
    from mintbot_mcp.access import GrantStore, Principal
    store = GrantStore(tmp_path / 'private' / 'grants.json')
    with pytest.raises(ValueError):
        store.issue('Guest', tools=[tool])
    assert not Principal(owner=False, allowed_tools=frozenset([tool])).allows_tool(tool)


def test_atomic_grant_write_syncs_directory_for_durable_revocation(tmp_path, monkeypatch):
    import stat
    from mintbot_mcp import access
    seen = []
    original = access.os.fsync
    def sync(fd):
        seen.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        original(fd)
    monkeypatch.setattr(access.os, 'fsync', sync)
    store = access.GrantStore(tmp_path / 'private' / 'grants.json')
    grant, _ = store.issue('Guest', tools=['chat_send'])
    assert True in seen
    seen.clear()
    assert store.revoke(grant['id'])
    assert True in seen


@pytest.mark.parametrize('ttl', [0, -1, float('inf'), float('nan'), 366 * 86400])
def test_invalid_expiry_is_refused(tmp_path, ttl):
    from mintbot_mcp.access import GrantStore
    with pytest.raises(ValueError):
        GrantStore(tmp_path / 'private' / 'grants.json').issue('Guest', tools=['chat_send'], ttl=ttl)


def test_unsafe_grant_file_mode_is_refused(tmp_path):
    from mintbot_mcp.access import GrantStore
    store = GrantStore(tmp_path / 'private' / 'grants.json')
    _, secret = store.issue('Guest', tools=['chat_send'])
    store.path.chmod(0o644)
    with pytest.raises(ValueError):
        store.authenticate(secret)


def test_project_grants_reject_global_tool_permissions(tmp_path):
    from mintbot_mcp.access import GrantStore
    store = GrantStore(tmp_path / 'private' / 'grants.json')
    with pytest.raises(ValueError):
        store.issue('Guest', tools=['model_get'], project_ids=['p1'])
    with pytest.raises(ValueError):
        store.issue('Guest', tools=['chat_send'], project_ids=[])
    with pytest.raises(ValueError):
        store.issue('Guest', tools=[])
