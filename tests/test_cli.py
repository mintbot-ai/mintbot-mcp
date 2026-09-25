"""The mintbot-mcp command."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mintbot_mcp import __version__, cli


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"mintbot-mcp {__version__}"


def test_token_command_creates_prints_and_rotates(tmp_path: Path, monkeypatch, capsys):
    token_file = tmp_path / "state" / "token"
    monkeypatch.setenv("MINTBOT_MCP_TOKEN_FILE", str(token_file))
    assert cli.main(["token"]) == 0
    first = capsys.readouterr().out.strip()
    assert token_file.read_text().strip() == first
    assert cli.main(["token"]) == 0 and capsys.readouterr().out.strip() == first
    assert cli.main(["token", "--rotate"]) == 0
    assert capsys.readouterr().out.strip() != first


def test_client_config_is_the_mcp_servers_block(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir()
    (tmp_path / "hermes" / "env").write_text("API_SERVER_KEY=k\n")
    monkeypatch.setenv("MINTBOT_MCP_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("MINTBOT_MCP_PORT", "8651")
    assert cli.main(["client-config"]) == 0
    block = json.loads(capsys.readouterr().out)
    server = block["mcpServers"]["mintbot"]
    assert server["type"] == "http" and server["url"] == "http://127.0.0.1:8651/mcp"
    assert server["headers"]["Authorization"] == "Bearer " + (tmp_path / "token").read_text().strip()

    assert cli.main(["client-config", "--url", "https://agent.example/mcp"]) == 0
    assert json.loads(capsys.readouterr().out)["mcpServers"]["mintbot"]["url"] == "https://agent.example/mcp"


def test_commands_needing_an_agent_fail_cleanly_elsewhere(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nowhere"))
    monkeypatch.delenv("MINTBOT_MCP_HERMES_API_KEY", raising=False)
    assert cli.main(["check"]) == 2
    assert "API_SERVER_KEY" in capsys.readouterr().err


async def test_check_reports_each_backend(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir()
    (tmp_path / "hermes" / "env").write_text("API_SERVER_KEY=k\n")
    monkeypatch.setenv("MINTBOT_MCP_HERMES_URL", "http://127.0.0.1:1")   # nothing listens on port 1
    monkeypatch.setenv("MINTBOT_MCP_LOCAL_API_URL", "http://127.0.0.1:1")
    results = await cli.probe(cli.load_settings())
    assert set(results) == {"hermes", "local_api", "central"}
    assert results["hermes"].startswith("error:") and results["local_api"].startswith("error:")
    assert "not configured" in results["central"]


def test_check_exit_code_follows_the_loopback_backends(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir()
    (tmp_path / "hermes" / "env").write_text("API_SERVER_KEY=k\n")

    async def fake_probe(_settings):
        return {"hermes": "ok", "local_api": "ok", "central": "error: not configured"}
    monkeypatch.setattr(cli, "probe", fake_probe)
    assert cli.main(["check"]) == 0
    assert "central: error: not configured" in capsys.readouterr().out

    async def broken_probe(_settings):
        return {"hermes": "error: down", "local_api": "ok", "central": "ok"}
    monkeypatch.setattr(cli, "probe", broken_probe)
    assert cli.main(["check"]) == 1
