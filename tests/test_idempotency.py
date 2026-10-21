"""Durable at-most-once claims, including separate processes."""
import concurrent.futures
import json
import multiprocessing
import os
import sqlite3

import pytest


def claim_worker(path):
    from mintbot_mcp.idempotency import IdempotencyStore
    return IdempotencyStore(path).reserve('alice', 'key', {'message': 'õun 🍎'})[0]


def test_atomic_claim_survives_process_restart(tmp_path):
    from mintbot_mcp.idempotency import IdempotencyStore
    path = tmp_path / 'private' / 'chat.sqlite'
    with concurrent.futures.ProcessPoolExecutor(2, mp_context=multiprocessing.get_context('fork')) as pool:
        results = list(pool.map(claim_worker, [path, path]))
    assert sorted(results) == [False, True]
    store = IdempotencyStore(path)
    created, saved = store.reserve('alice', 'key', {'message': 'õun 🍎'})
    assert not created and saved['status'] == 'unknown'
    assert saved['code'] == 'outcome_unknown'
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_payload_conflict_and_credential_scope(tmp_path):
    from mintbot_mcp.idempotency import IdempotencyStore
    from mintbot_mcp.backends import BackendError
    store = IdempotencyStore(tmp_path / 'state' / 'chat.sqlite')
    _, first = store.reserve('alice', 'key', {'message': 'hello'})
    with pytest.raises(BackendError, match='different'):
        store.reserve('alice', 'key', {'message': 'changed'})
    assert store.reserve('bob', 'key', {'message': 'changed'})[0]
    store.save(first['turn_id'], {'ok': True, 'status': 'done', 'reply': 'vastus 🌻', 'turn_id': first['turn_id']})
    assert store.reserve('alice', 'key', {'message': 'hello'})[1]['reply'] == 'vastus 🌻'


def test_replies_expire_on_read_without_forgetting_dedupe(tmp_path, monkeypatch):
    from mintbot_mcp import idempotency
    clock = [1000.0]
    monkeypatch.setattr(idempotency.time, 'time', lambda: clock[0])
    store = idempotency.IdempotencyStore(tmp_path / 'state' / 'chat.sqlite')
    _, first = store.reserve('alice', 'key', {'message': 'hello'})
    store.save(first['turn_id'], {'ok': True, 'status': 'done', 'reply': 'private reply', 'turn_id': first['turn_id']})
    clock[0] += idempotency.REPLY_TTL + 1
    fresh, saved = store.reserve('alice', 'key', {'message': 'hello'})
    assert not fresh and saved['reply_expired'] and 'reply' not in saved
    assert saved['turn_id'] == first['turn_id']


def test_reply_count_and_size_are_bounded_without_evicting_running(tmp_path):
    from mintbot_mcp.idempotency import IdempotencyStore, MAX_REPLIES, MAX_REPLY_CHARS
    store = IdempotencyStore(tmp_path / 'state' / 'chat.sqlite')
    _, running = store.reserve('alice', 'running', {})
    store.save(running['turn_id'], {'status': 'running', 'turn_id': running['turn_id'], 'session_id': 's1'})
    for number in range(MAX_REPLIES + 2):
        _, saved = store.reserve('alice', str(number), {})
        store.save(saved['turn_id'], {'status': 'done', 'turn_id': saved['turn_id'], 'reply': 'x' * (MAX_REPLY_CHARS + 1)})
    with sqlite3.connect(store.path) as db:
        snapshots = [json.loads(r[0]) for r in db.execute('SELECT snapshot FROM claims')]
    assert sum('reply' in s for s in snapshots) == MAX_REPLIES
    assert all(len(s.get('reply', '')) <= MAX_REPLY_CHARS for s in snapshots)
    assert store.reserve('alice', 'running', {})[1]['session_id'] == 's1'
    assert store.reserve('alice', 'running', {})[1]['status'] == 'unknown'
