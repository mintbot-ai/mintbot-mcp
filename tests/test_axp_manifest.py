"""The AXP manifest: valid against the protocol schema AND the reference
implementation, every referenced file exists, and the security-relevant
facts a host shows on its consent card are what we mean them to be.

The spec repo (mintbot-ai/agent-extension) is expected as a sibling checkout
(../agent-extension) or at $AXP_SPEC_DIR; its `axp` package is imported from
there when installed in the active environment.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

import jsonschema
import pytest

from mintbot_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = Path(os.environ.get("AXP_SPEC_DIR", ROOT.parent / "agent-extension"))
AXP_SCHEMA = SPEC_DIR / "schema" / "agent-extension.schema.json"
KEY_ID = "mintbot.ai-mintbot-mcp-2026"


def manifest() -> dict:
    return json.loads((ROOT / "agent-extension.json").read_text(encoding="utf-8"))


def test_manifest_validates_against_protocol_schema():
    assert AXP_SCHEMA.exists(), f"AXP schema not found at {AXP_SCHEMA} (set AXP_SPEC_DIR)"
    schema = json.loads(AXP_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(manifest(), schema)


def test_manifest_validates_with_reference_implementation():
    axp_manifest = pytest.importorskip("axp.manifest")
    summary = axp_manifest.validate(manifest())
    assert summary["ext_id"] == "mintbot.ai/mintbot-mcp"
    assert summary["publisher_derived"] is False
    assert summary["runtimes"] == ["hermes"]
    assert list(summary["provides"]) == ["services"]


def test_one_version_everywhere():
    m = manifest()
    version = m["identity"]["version"]
    assert re.match(r"^\d+\.\d+\.\d+$", version)
    assert version == __version__, "mintbot_mcp.__version__ and the manifest must agree (scripts/release.sh stamps both)"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == version
    for url in (m["release"]["artifact"]["url"], m["targets"][0]["delivery"]["url"]):
        assert url == f"https://github.com/mintbot-ai/mintbot-mcp/releases/download/v{version}/mintbot-mcp-{version}.tar.gz"
    assert m["release"]["changelog"] == f"https://github.com/mintbot-ai/mintbot-mcp/releases/tag/v{version}"
    assert m["updates"]["source"] == {"kind": "github", "repo": "mintbot-ai/mintbot-mcp"}
    assert m["targets"][0]["delivery"]["method"] == "archive"


def test_permissions_are_honest_and_minimal():
    """What the consent card will show: no root, one loopback listener, egress
    limited to PyPI and the mintbot API, read-only Hermes home, writable state."""
    perms = manifest()["permissions"]
    assert perms["root"] is False
    host_re = re.compile(r"^[a-z0-9.-]+:443$")
    assert all(host_re.match(h) for h in perms["network_egress"]), perms["network_egress"]
    assert {"pypi.org:443", "files.pythonhosted.org:443", "api.mintbot.ai:443"} <= set(perms["network_egress"])
    assert perms["network_ingress"] == ["127.0.0.1:8650/tcp"], "the endpoint is loopback only"
    assert set(perms["filesystem"]) == {"config:r", "state:rw"}
    assert isinstance(perms["reason"], dict)
    assert {"network_egress", "network_ingress", "filesystem", "runtime"} <= set(perms["reason"])


def test_the_daemon_is_bound_for_the_host_to_own():
    """SPEC section 5.3: a `command` binding means the host writes, contains
    and supervises the unit; install.sh must then not start the daemon."""
    target = manifest()["targets"][0]
    services = manifest()["provides"]["services"]
    assert [s["name"] for s in services] == ["mcp"] and services[0]["kind"] == "daemon"
    assert services[0]["endpoint"] == "http://127.0.0.1:8650/mcp"
    binding = target["component_map"]["services"]
    assert binding["command"] == ["bin/mintbot-mcp", "serve"]
    assert binding["restart"] == "always"
    assert "mcp_servers" not in manifest()["provides"], "the server is for external clients, not for the agent's own MCP config"
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'if [ -n "${AXP_HOST:-}" ]' in install, "install.sh must leave the daemon to an AXP host"


def test_signed_by_the_publisher_key():
    m = manifest()
    assert m["signing"]["public_key"].startswith("ed25519:")
    assert m["signing"]["key_id"] == KEY_ID
    axp_signing = pytest.importorskip("axp.signing")
    assert axp_signing.verify_manifest(m) is True, "manifest signature must verify against its own key"
    digest = m["release"]["artifact"]["sha256"]
    assert digest == m["targets"][0]["delivery"]["sha256"] and digest != "0" * 64, "release.sh must have filled the digest"


def test_every_manifest_reference_exists():
    refs = list(manifest()["targets"][0]["lifecycle"].values())
    missing = [ref for ref in refs if not (ROOT / ref).exists()]
    assert missing == []


def test_artifact_allowlist_covers_every_reference_and_the_package():
    build = (ROOT / "scripts" / "build-artifact.sh").read_text(encoding="utf-8")
    listed = set(re.search(r"RUNTIME_FILES=\((.*?)\)", build, re.S).group(1).split())
    needed = set(manifest()["targets"][0]["lifecycle"].values())
    needed |= {f"mintbot_mcp/{p.name}" for p in (ROOT / "mintbot_mcp").glob("*.py")}
    needed |= {"requirements.txt", "README.md", "LICENSE", "after-install.md"}
    assert needed <= listed, sorted(needed - listed)
    assert "agent-extension.json" not in listed, "the manifest carries the artifact digest; it cannot be inside"
    assert not [f for f in listed if f.startswith("tests/") or f.endswith(".key")]


def test_lifecycle_scripts_follow_the_host_contract():
    """SPEC section 6: scripts honour AXP_PREFIX / AXP_STATE_DIR / AXP_CACHE_DIR /
    AXP_PURGE / AXP_FROM_VERSION, never the pre-spec AXP_INSTALL_DIR, and
    health uses the 0/1/2 exit codes."""
    scripts = {name: (ROOT / name).read_text(encoding="utf-8")
               for name in ("install.sh", "upgrade.sh", "uninstall.sh", "healthcheck.sh")}
    assert not any("AXP_INSTALL_DIR" in body for body in scripts.values())
    for name in ("install.sh", "uninstall.sh", "healthcheck.sh"):
        assert "AXP_PREFIX" in scripts[name] and "AXP_STATE_DIR" in scripts[name], name
    assert "AXP_CACHE_DIR" in scripts["install.sh"]
    assert "AXP_PURGE" in scripts["uninstall.sh"]
    assert "AXP_FROM_VERSION" in scripts["upgrade.sh"]
    assert "exit 2" in scripts["healthcheck.sh"] and "exit 1" in scripts["healthcheck.sh"]


def keys_directory() -> dict:
    return json.loads((ROOT / "agent-extension-keys.json").read_text(encoding="utf-8"))


def test_key_directory_validates_against_protocol_schema():
    schema_path = SPEC_DIR / "schema" / "agent-extension-keys.schema.json"
    assert schema_path.exists(), f"AXP keys schema not found at {schema_path} (set AXP_SPEC_DIR)"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(keys_directory(), schema)


def test_key_directory_vouches_for_the_signing_key():
    """SPEC section 8.5: the directory served from https://mintbot.ai/.well-known/
    must list the key the manifest is signed with, for this extension, and not
    as revoked -- otherwise every host treats the next release as suspicious."""
    m = manifest()
    directory = keys_directory()
    assert directory["publisher"] == m["identity"]["publisher"]
    axp_signing = pytest.importorskip("axp.signing")
    listed = axp_signing.parse_key_directory(directory, publisher=m["identity"]["publisher"], name=m["identity"]["name"])
    assert m["signing"]["public_key"] in listed
    entry = next(k for k in directory["keys"] if k["public_key"] == m["signing"]["public_key"])
    assert entry.get("key_id") == KEY_ID and not entry.get("revoked")


def test_key_directory_matches_the_other_publisher_copies():
    """One mintbot.ai directory, three checkouts (this repo, graph-memory, the
    marketing site that serves it). A key listed in one and not the others
    breaks rotation, so they must not drift when checked out side by side."""
    ours = keys_directory()
    siblings = [ROOT.parent / "graph-memory" / "agent-extension-keys.json",
                ROOT.parent / "mintbot" / "website" / "dist" / ".well-known" / "agent-extension-keys.json"]
    present = [p for p in siblings if p.is_file()]
    if not present:
        pytest.skip("no sibling publisher copy checked out next to this repo")
    for path in present:
        assert json.loads(path.read_text(encoding="utf-8")) == ours, f"{path} drifted from ours"
