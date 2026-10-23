"""Local durable grants and request-scoped authorization (not an OS sandbox)."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import stat
import time


class AccessDenied(PermissionError):
    """The credential cannot access this operation or project."""


# These routes change the owner's own controls of the agent (the panel's
# two-factor state, the persona, the Telegram bot, what gets installed), and
# the server reaches them as the agent's machine identity. Never delegate them.
OWNER_ONLY = frozenset({
    'security_state', 'persona_set',
    'telegram_bot_apply', 'telegram_bot_remove', 'extensions_install',
})
PROJECT_TOOLS = frozenset({
    'chat_send', 'chat_wait', 'chat_cancel', 'chat_sessions',
    'chat_session_messages', 'chat_session_delete', 'chat_session_rename',
    'projects_list', 'project_get',
})
TOOL_GROUPS = {
    'chat': frozenset({'chat_send', 'chat_wait', 'chat_cancel', 'chat_sessions',
                       'chat_session_messages', 'chat_session_delete', 'chat_session_rename'}),
    'projects': frozenset({'projects_list', 'project_get'}),
}


@dataclass(frozen=True)
class Principal:
    credential_id: str = 'owner'
    name: str = 'Owner'
    owner: bool = True
    allowed_tools: frozenset[str] = frozenset()
    project_ids: frozenset[str] | None = None
    expires_at: float | None = None

    @property
    def project_restricted(self) -> bool:
        return not self.owner and self.project_ids is not None

    def allows_tool(self, name: str) -> bool:
        return self.owner or (name in self.allowed_tools and name not in OWNER_ONLY
                              and (not self.project_restricted or name in PROJECT_TOOLS))

    def require_tool(self, name: str) -> None:
        if not self.allows_tool(name):
            raise AccessDenied('credential does not allow this tool')

    def require_project(self, project_id: str | None) -> None:
        if (not self.owner and self.project_ids is not None
                and (project_id is None or str(project_id) not in self.project_ids)):
            raise AccessDenied('credential does not allow this project')


principal_context: ContextVar[Principal] = ContextVar('mcp_principal', default=Principal())


def current_principal() -> Principal:
    return principal_context.get()


class GrantStore:
    """0600 JSON behind a 0700 directory; flock + atomic replace prevent lost updates.

    In production the extension runs as root, so these files are root-only.
    Non-root local development uses the same owner-only permissions.
    """
    def __init__(self, path: Path):
        self.path = Path(path)

    def _secure_dir(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('grant directory must be owned by the daemon user')
        self.path.parent.chmod(0o700)

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {'grants': []}
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('grant state must be owner-only')
            return json.load(stream)

    def _mutate(self, fn):
        self._secure_dir()
        fd = os.open(self.path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            result = fn(data)
            temporary = self.path.with_name('.grants-' + secrets.token_hex(12))
            outfd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(outfd, 'w') as stream:
                    json.dump(data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
            return result

    def issue(self, name: str, *, tools=(), groups=(), project_ids=None, ttl=3600, now=None):
        if not name.strip() or len(name) > 100:
            raise ValueError('name must be 1-100 characters')
        if not math.isfinite(ttl) or ttl <= 0 or ttl > 365 * 86400:
            raise ValueError('ttl must be positive and at most 365 days')
        allowed = set(tools)
        for group in groups:
            if group not in TOOL_GROUPS:
                raise ValueError('unknown tool group')
            allowed.update(TOOL_GROUPS[group])
        if not allowed or any(not isinstance(t, str) or not t or len(t) > 128 for t in allowed):
            raise ValueError('an explicit nonempty tool allowlist is required')
        if allowed & OWNER_ONLY:
            raise ValueError('owner-only tools cannot be delegated')
        projects = None if project_ids is None else sorted(set(str(p) for p in project_ids))
        if projects is not None and (not projects or any(not p or len(p) > 128 for p in projects)):
            raise ValueError('project IDs must be a nonempty list')
        if projects is not None and not allowed <= PROJECT_TOOLS:
            raise ValueError('project-scoped grants may only use project-aware tools')
        secret = 'mbg_' + secrets.token_urlsafe(32)
        record = {'id': secrets.token_hex(12), 'name': name.strip(),
                  'secret_hash': hashlib.sha256(secret.encode()).hexdigest(),
                  'tools': sorted(allowed), 'project_ids': projects,
                  'expires_at': (time.time() if now is None else now) + ttl, 'revoked': False}
        self._mutate(lambda data: data['grants'].append(record))
        return {k: v for k, v in record.items() if k != 'secret_hash'}, secret

    def list(self):
        return [{k: v for k, v in g.items() if k != 'secret_hash'} for g in self._read()['grants']]

    def revoke(self, credential_id: str):
        def update(data):
            for grant in data['grants']:
                if grant['id'] == credential_id:
                    grant['revoked'] = True
                    return True
            return False
        return self._mutate(update)

    def authenticate(self, secret: str, *, now=None) -> Principal | None:
        digest = hashlib.sha256(secret.encode()).hexdigest()
        timestamp = time.time() if now is None else now
        for grant in self._read()['grants']:
            if hmac.compare_digest(digest, grant['secret_hash']) and not grant['revoked'] and timestamp < grant['expires_at']:
                return Principal(grant['id'], grant['name'], False, frozenset(grant['tools']),
                                 None if grant['project_ids'] is None else frozenset(grant['project_ids']),
                                 grant['expires_at'])
        return None
