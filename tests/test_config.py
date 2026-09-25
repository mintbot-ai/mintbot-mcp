"""Settings come from files that already exist on a mintbot agent VPS."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mintbot_mcp import config


def write_home(home: Path, *, env: str = "API_SERVER_KEY=secret-key\n", cfg: str | None = None) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / "env").write_text(env, encoding="utf-8")
    if cfg is not None:
        (home / "config.yaml").write_text(cfg, encoding="utf-8")
    return home


def test_read_env_file_handles_export_quotes_and_comments(tmp_path: Path):
    path = tmp_path / "env"
    path.write_text('# comment\nexport A=1\nB="two words"\nC=\'x=y\'\nD = spaced \nnot a pair\n', encoding="utf-8")
    assert config.read_env_file(path) == {"A": "1", "B": "two words", "C": "x=y", "D": "spaced"}
    assert config.read_env_file(tmp_path / "missing") == {}


def test_load_settings_from_a_mintbot_agent_home(tmp_path: Path, monkeypatch):
    for key in ("MINTBOT_MCP_HERMES_URL", "MINTBOT_MCP_LOCAL_API_URL", "MINTBOT_MCP_HOST", "MINTBOT_MCP_PORT",
                "MINTBOT_MCP_HERMES_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    home = write_home(tmp_path / "hermes", env="API_SERVER_KEY=secret-key\nAPI_SERVER_PORT=9999\nAGENT_ID=7\n",
                      cfg="mintbot:\n  api_base_url: https://api.mintbot.ai/\n  panel_token: tok\n  agent_id: 1091\n")
    s = config.load_settings(home=home)
    assert s.hermes_url == "http://127.0.0.1:9999" and s.hermes_api_key == "secret-key"
    assert s.local_api_url == "http://127.0.0.1:8643"
    assert s.central_url == "https://api.mintbot.ai" and s.panel_token == "tok" and s.agent_id == 1091
    assert s.central_available is True
    assert (s.host, s.port) == ("127.0.0.1", 8650)


def test_load_settings_without_central_or_config(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MINTBOT_MCP_HERMES_API_KEY", raising=False)
    home = write_home(tmp_path / "hermes", env="API_SERVER_KEY=k\nAGENT_ID=notanumber\n")
    s = config.load_settings(home=home)
    assert s.central_url is None and s.panel_token is None and s.agent_id is None
    assert s.central_available is False


def test_load_settings_refuses_a_non_agent_home(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MINTBOT_MCP_HERMES_API_KEY", raising=False)
    with pytest.raises(config.ConfigError, match="API_SERVER_KEY"):
        config.load_settings(home=tmp_path / "empty")


def test_broken_config_yaml_is_not_fatal(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MINTBOT_MCP_HERMES_API_KEY", raising=False)
    home = write_home(tmp_path / "hermes", cfg="mintbot: [unclosed\n")
    assert config.load_settings(home=home).central_url is None


def test_environment_overrides_win(tmp_path: Path, monkeypatch):
    home = write_home(tmp_path / "hermes")
    monkeypatch.setenv("MINTBOT_MCP_HERMES_URL", "http://h:1")
    monkeypatch.setenv("MINTBOT_MCP_LOCAL_API_URL", "http://l:2")
    monkeypatch.setenv("MINTBOT_MCP_HERMES_API_KEY", "override")
    monkeypatch.setenv("MINTBOT_MCP_PORT", "7000")
    s = config.load_settings(home=home)
    assert (s.hermes_url, s.local_api_url, s.hermes_api_key, s.port) == ("http://h:1", "http://l:2", "override", 7000)
    assert s.with_listener("0.0.0.0", None).host == "0.0.0.0"
    assert s.with_listener(None, 1).port == 1


def test_hermes_home_and_state_dir_follow_the_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert config.hermes_home() == tmp_path / "h"
    monkeypatch.delenv("HERMES_HOME")
    monkeypatch.setenv("AXP_HERMES_HOME", str(tmp_path / "axp-h"))
    assert config.hermes_home() == tmp_path / "axp-h"

    monkeypatch.setenv("AXP_STATE_DIR", str(tmp_path / "axp-state"))
    monkeypatch.delenv("MINTBOT_MCP_STATE_DIR", raising=False)
    monkeypatch.delenv("MINTBOT_MCP_TOKEN_FILE", raising=False)
    assert config.token_file() == tmp_path / "axp-state" / "token"
    monkeypatch.setenv("MINTBOT_MCP_STATE_DIR", str(tmp_path / "own"))
    assert config.state_dir() == tmp_path / "own"


def test_ensure_token_is_private_stable_and_replaceable(tmp_path: Path):
    path = tmp_path / "deep" / "state" / "token"
    first = config.ensure_token(path)
    assert len(first) >= 40 and path.read_text().strip() == first
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert config.ensure_token(path) == first, "a second call must not rotate"
    path.unlink()
    assert config.ensure_token(path) != first
    assert not list(path.parent.glob("*.tmp"))
