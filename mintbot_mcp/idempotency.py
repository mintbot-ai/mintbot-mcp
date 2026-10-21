"""Private durable at-most-once chat claims.

Dedupe tombstones live for the lifetime of this state file: keys must never
be reused. Reply bodies are retained for 15 minutes, at most 32 completed
replies of at most 256 Ki characters. Running claims are never evicted.
A crash between reservation and completion has an unknown outcome, not a
license to repeat potentially expensive or destructive agent side effects.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import closing

from .backends import BackendError

REPLY_TTL = 900
MAX_REPLIES = 32
MAX_REPLY_CHARS = 262144


class IdempotencyStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS claims (credential TEXT, key TEXT, fingerprint TEXT NOT NULL, '
                       'turn_id TEXT UNIQUE NOT NULL, snapshot TEXT NOT NULL, updated REAL NOT NULL, '
                       'PRIMARY KEY (credential, key))')

    def _connect(self):
        return closing(sqlite3.connect(self.path, timeout=10))

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()

    def reserve(self, credential: str, key: str, payload: dict):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            identity = (self._digest(credential), self._digest(key))
            fingerprint = self._digest(payload)
            row = db.execute('SELECT fingerprint, snapshot, updated FROM claims WHERE credential=? AND key=?', identity).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise BackendError('idempotency_key was already used with a different payload', code='idempotency_conflict')
                snapshot = json.loads(row[1])
                if snapshot['status'] not in ('running', 'unknown') and time.time() - row[2] > REPLY_TTL:
                    snapshot.pop('reply', None)
                    snapshot['reply_expired'] = True
                    db.execute('UPDATE claims SET snapshot=? WHERE credential=? AND key=?',
                               (json.dumps(snapshot), *identity))
                    db.commit()
                if snapshot['status'] == 'running':
                    snapshot.update(ok=False, status='unknown', code='outcome_unknown',
                                    error='Turn may still be running in another process or was interrupted; not restarted.')
                return False, snapshot
            snapshot = {'ok': False, 'turn_id': uuid.uuid4().hex, 'session_id': None,
                        'status': 'unknown', 'code': 'outcome_unknown', 'reply': '',
                        'error': 'Reserved request has an uncertain outcome; it will not be restarted.'}
            db.execute('INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?)',
                       (*identity, fingerprint, snapshot['turn_id'], json.dumps(snapshot), time.time()))
            db.commit()
            return True, snapshot

    def lookup(self, credential: str, turn_id: str):
        """Read only a turn owned by this credential; never reconstruct a task."""
        with self._connect() as db:
            row = db.execute('SELECT snapshot, updated FROM claims WHERE credential=? AND turn_id=?',
                             (self._digest(credential), turn_id)).fetchone()
            if row is None:
                return None
            snapshot = json.loads(row[0])
            if snapshot['status'] == 'running':
                snapshot.update(ok=False, status='unknown', code='outcome_unknown',
                                error='Turn may still be running in another process or was interrupted; not restarted.')
            elif snapshot['status'] != 'unknown' and time.time() - row[1] > REPLY_TTL:
                snapshot.pop('reply', None)
                snapshot['reply_expired'] = True
                db.execute('UPDATE claims SET snapshot=? WHERE turn_id=?', (json.dumps(snapshot), turn_id))
                db.commit()
            return snapshot

    def save(self, turn_id: str, snapshot: dict):
        snapshot = dict(snapshot)
        if len(snapshot.get('reply', '')) > MAX_REPLY_CHARS:
            snapshot['reply'] = snapshot['reply'][:MAX_REPLY_CHARS]
            snapshot['reply_truncated'] = True
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE claims SET snapshot=?, updated=? WHERE turn_id=?',
                       (json.dumps(snapshot, ensure_ascii=False), now, turn_id))
            rows = db.execute('SELECT turn_id, snapshot, updated FROM claims ORDER BY updated DESC').fetchall()
            completed = 0
            for tid, encoded, updated in rows:
                saved = json.loads(encoded)
                if saved['status'] in ('running', 'unknown'):
                    continue
                completed += 1
                if completed > MAX_REPLIES or now - updated > REPLY_TTL:
                    saved.pop('reply', None)
                    saved['reply_expired'] = True
                    db.execute('UPDATE claims SET snapshot=? WHERE turn_id=?', (json.dumps(saved), tid))
            db.commit()
