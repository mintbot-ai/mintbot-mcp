"""Where the daemon finds the agent it serves.

Everything comes from files that already exist on a mintbot agent VPS:
``$HERMES_HOME/env`` (the Hermes API key and port), ``$HERMES_HOME/config.yaml``
(the ``mintbot:`` block with the central API base and the panel token) and the
extension's own state dir (the bearer token MCP clients present). Nothing is
configured twice, and nothing here talks to the network.
"""
from __future__ import annotations

import dataclasses
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8650
HERMES_API_PORT = 8642
LOCAL_API_PORT = 8643
TOKEN_BYTES = 32
PUBLISHER = "mintbot.ai"
NAME = "mintbot-mcp"


class ConfigError(RuntimeError):
    """The VPS does not look like a mintbot agent (or the daemon is misconfigured)."""


def hermes_home() -> Path:
    for key in ("HERMES_HOME", "AXP_HERMES_HOME"):
        value = os.environ.get(key)
        if value:
            return Path(value)
    # A host-owned unit points HOME at the state dir; the agent itself lives under root.
    if os.geteuid() == 0:
        return Path("/root/.hermes")
    return Path.home() / ".hermes"


def state_dir() -> Path:
    value = os.environ.get("MINTBOT_MCP_STATE_DIR") or os.environ.get("AXP_STATE_DIR")
    if value:
        return Path(value)
    if os.geteuid() == 0:
        return Path("/var/lib/axp") / PUBLISHER / NAME
    return Path.home() / ".local" / "state" / "axp" / PUBLISHER / NAME


def token_file() -> Path:
    value = os.environ.get("MINTBOT_MCP_TOKEN_FILE")
    return Path(value) if value else state_dir() / "token"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file: ``KEY=value``, optional ``export``, optional quotes."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key.strip()] = value
    return out


@dataclass(frozen=True)
class Settings:
    hermes_url: str
    hermes_api_key: str
    local_api_url: str
    central_url: str | None
    panel_token: str | None
    agent_id: int | None
    token_file: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def central_available(self) -> bool:
        return bool(self.central_url and self.panel_token)

    def with_listener(self, host: str | None, port: int | None) -> "Settings":
        return dataclasses.replace(self, host=host or self.host, port=port or self.port)


def load_settings(*, home: Path | None = None) -> Settings:
    home = home or hermes_home()
    env = read_env_file(home / "env")
    api_key = (
        os.environ.get("MINTBOT_MCP_HERMES_API_KEY")
        or env.get("API_SERVER_KEY")
        or env.get("PROXY_TOKEN")
        or ""
    )
    if not api_key:
        raise ConfigError(f"no API_SERVER_KEY in {home / 'env'}; is this a mintbot agent VPS?")
    hermes_port = env.get("API_SERVER_PORT") or str(HERMES_API_PORT)
    mintbot = _mintbot_block(home / "config.yaml")
    central_url = str(mintbot.get("api_base_url") or env.get("MINTBOT_API_BASE") or "").rstrip("/")
    agent_raw = mintbot.get("agent_id") or env.get("AGENT_ID")
    try:
        agent_id = int(agent_raw) if agent_raw not in (None, "") else None
    except (TypeError, ValueError):
        agent_id = None
    return Settings(
        hermes_url=os.environ.get("MINTBOT_MCP_HERMES_URL") or f"http://127.0.0.1:{hermes_port}",
        hermes_api_key=api_key,
        local_api_url=os.environ.get("MINTBOT_MCP_LOCAL_API_URL") or f"http://127.0.0.1:{LOCAL_API_PORT}",
        central_url=central_url or None,
        panel_token=str(mintbot.get("panel_token") or "") or None,
        agent_id=agent_id,
        token_file=token_file(),
        host=os.environ.get("MINTBOT_MCP_HOST") or DEFAULT_HOST,
        port=int(os.environ.get("MINTBOT_MCP_PORT") or DEFAULT_PORT),
    )


def _mintbot_block(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    block = data.get("mintbot") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def ensure_token(path: Path) -> str:
    """The bearer token MCP clients present: created once, mode 0600, never rotated implicitly."""
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    os.replace(tmp, path)
    return token
