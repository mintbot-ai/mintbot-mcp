"""Local and VPS identity discovery, with no legacy API key prerequisite."""
import json
from pathlib import Path

import httpx
import pytest

from mintbot_mcp import cli, config
from mintbot_mcp.backends import BackendError, HermesClient, LocalApiClient


@pytest.fixture
def local_installation(tmp_path, monkeypatch):
    root = tmp_path / "installation"
    home = root / "hermes-home"
    home.mkdir(parents=True)
    (home / "env").write_text("MINTBOT_AGENT_ID=9019\n")
    credentials = root / "state/security/local_credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(json.dumps({"current": {"hermes": "initial"}, "activated": False}))
    monkeypatch.setenv("MINTBOT_AGENT_HOME", str(root))
    return root, credentials


def test_local_home_discovery_without_hermes_home(local_installation):
    root, _ = local_installation
    assert config.hermes_home() == root / "hermes-home"
    assert config.load_settings().agent_id == 9019


def test_vps_credentials_default_is_unchanged():
    assert config.local_credentials_file() == Path("/var/lib/mintbot-agent/security/local_credentials.json")


def test_explicit_identity_supports_vps_without_legacy_key(local_installation, monkeypatch, tmp_path):
    _, credentials = local_installation
    monkeypatch.delenv("MINTBOT_AGENT_HOME")
    monkeypatch.setenv("MINTBOT_MCP_LOCAL_CREDENTIALS_FILE", str(credentials))
    settings = config.load_settings(home=tmp_path)
    assert settings.local_credentials_file == credentials
    assert settings.local_key() == settings.hermes_api_key == "initial"


def test_identity_override_wins_over_local_layout(local_installation, monkeypatch, tmp_path):
    override = tmp_path / "override.json"
    override.write_text(json.dumps({"current": {"hermes": "override"}}))
    monkeypatch.setenv("MINTBOT_MCP_LOCAL_CREDENTIALS_FILE", str(override))
    assert config.load_settings().local_key() == "override"


@pytest.mark.parametrize("root", ["", "/", "relative", "/installation/../other"])
def test_invalid_local_root_is_rejected(root, monkeypatch, tmp_path):
    monkeypatch.setenv("MINTBOT_AGENT_HOME", root)
    with pytest.raises(config.ConfigError, match="MINTBOT_AGENT_HOME"):
        config.load_settings(home=tmp_path)


@pytest.mark.parametrize("body", [None, "not-json", "{}", "[]", '{"current": {"hermes": ""}}',
                                 '{"current": {"hermes": "old"}, "activated": true}'])
def test_missing_or_malformed_local_identity_fails_closed(local_installation, body):
    _, credentials = local_installation
    if body is None:
        credentials.unlink()
    else:
        credentials.write_text(body)
    with pytest.raises(config.ConfigError, match="active local identity"):
        config.load_settings()


def test_serve_cli_accepts_modern_local_installation(local_installation, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_serve", seen.append)
    assert cli.main(["serve"]) == 0
    assert len(seen) == 1 and seen[0].local_key() == "initial"


async def test_discovered_identity_rotates_on_both_backends(local_installation):
    _, credentials = local_installation
    settings = config.load_settings()
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={})

    hermes = HermesClient(settings, transport=httpx.MockTransport(handler))
    local = LocalApiClient(settings, transport=httpx.MockTransport(handler))
    try:
        await hermes.list_sessions()
        await local.project_request("GET", "/mcp/projects")
        credentials.write_text(json.dumps({"current": {"hermes": "initial"},
                                           "pending": {"hermes": "successor"}, "activated": True}))
        await hermes.list_sessions()
        await local.project_request("GET", "/mcp/projects")
        assert seen == ["Bearer initial", "Bearer initial", "Bearer successor", "Bearer successor"]
        credentials.unlink()
        with pytest.raises(BackendError, match="active local identity"):
            await hermes.list_sessions()
        with pytest.raises(BackendError, match="active local identity"):
            await local.project_request("GET", "/mcp/projects")
        assert len(seen) == 4
    finally:
        await hermes.aclose()
        await local.aclose()
