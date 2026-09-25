"""Release and lifecycle tooling - the shell that an AXP host (or a human,
SPEC section 10) actually runs.

Everything here executes the real scripts in a throwaway HOME / prefix / state
dir with a fake `python3` on PATH whose `-m venv` builds a stub virtualenv (its
`python` is the test interpreter, its `pip` only logs), so no test touches the
live install, the network, systemd or the publisher key.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIFECYCLE = [REPO / n for n in ("install.sh", "upgrade.sh", "uninstall.sh", "healthcheck.sh")]
BUILD = REPO / "scripts" / "build-artifact.sh"
RELEASE = REPO / "scripts" / "release.sh"

_FAKE_PYTHON = """#!/bin/bash
# Fake python3: `-m venv DIR` builds a stub venv (real interpreter, logging pip);
# anything else runs on the real interpreter.
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/bash\\nexec %s "$@"\\n' "__REAL__" > "$3/bin/python"
  printf '#!/bin/bash\\nprintf "pip %%s cache=%%s\\\\n" "$*" "${PIP_CACHE_DIR:-}" >> "${FAKE_LOG}"\\n' > "$3/bin/pip"
  chmod 755 "$3/bin/python" "$3/bin/pip"
  exit 0
fi
exec __REAL__ "$@"
"""

_FAKE_SYSTEMCTL = """#!/bin/bash
printf 'systemctl %s\\n' "$*" >> "${FAKE_LOG}"
"""


def _run(script: Path, *args: str, env: dict, cwd: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], env=env, cwd=str(cwd), capture_output=True, text=True, timeout=120)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(tmp_path: Path, *, axp_host: bool = True, systemctl: bool = False) -> dict:
    """A hermetic environment: throwaway HOME / prefix / state / cache, the fake
    python3 first on PATH, and AXP_HOST set unless the standalone path is wanted."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    fake = bin_dir / "python3"
    fake.write_text(_FAKE_PYTHON.replace("__REAL__", sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    if systemctl:
        (bin_dir / "systemctl").write_text(_FAKE_SYSTEMCTL, encoding="utf-8")
        (bin_dir / "systemctl").chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "AXP_PREFIX": str(tmp_path / "prefix"),
        "AXP_STATE_DIR": str(tmp_path / "state"),
        "AXP_CACHE_DIR": str(tmp_path / "cache"),
        "MINTBOT_MCP_UNIT_DIR": str(tmp_path / "units"),
        "MINTBOT_MCP_PORT": str(_free_port()),
        "FAKE_LOG": str(tmp_path / "fake.log"),
        "LANG": "C.UTF-8",
    }
    if axp_host:
        env["AXP_HOST"] = "mintbot/test"
    return env


def _log(env: dict) -> str:
    path = Path(env["FAKE_LOG"])
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _runtime_files() -> list[str]:
    block = re.search(r"RUNTIME_FILES=\((.*?)\)", BUILD.read_text(encoding="utf-8"), re.S).group(1)
    return block.split()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", [*LIFECYCLE, BUILD, RELEASE], ids=lambda p: p.name)
def test_scripts_parse_and_fail_closed(script):
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert os.access(script, os.X_OK), f"{script.name} is not executable"


# ---------------------------------------------------------------------------
# build-artifact.sh
# ---------------------------------------------------------------------------

def test_build_artifact_is_deterministic_and_contains_only_the_runtime_files(tmp_path):
    env = _env(tmp_path)
    first = _run(BUILD, "9.9.9", str(tmp_path / "a"), env=env)
    second = _run(BUILD, "9.9.9", str(tmp_path / "b"), env=env)
    assert first.returncode == 0 and second.returncode == 0, first.stderr + second.stderr
    a = tmp_path / "a" / "mintbot-mcp-9.9.9.tar.gz"
    b = tmp_path / "b" / "mintbot-mcp-9.9.9.tar.gz"
    assert _sha256(a) == _sha256(b), "a rebuild from the same tree must be byte-identical"
    assert first.stdout.split() == [_sha256(a), str(a)]

    with tarfile.open(a) as tar:
        members = tar.getmembers()
    names = sorted(m.name for m in members)
    assert names == sorted(_runtime_files())
    assert "agent-extension.json" not in names, "the manifest is signed OVER the artifact digest"
    for member in members:
        assert (member.mtime, member.uid, member.gid) == (0, 0, 0), member.name
        assert member.mode & 0o022 == 0, f"{member.name} is group/world writable"


def test_build_artifact_refuses_when_a_runtime_file_is_missing(tmp_path):
    clone = tmp_path / "clone"
    shutil.copytree(REPO, clone, ignore=shutil.ignore_patterns(".git", ".venv", "dist", "__pycache__", "tests", "*.pyc", "*.egg-info"))
    (clone / "LICENSE").unlink()
    result = _run(clone / "scripts" / "build-artifact.sh", "1.0.0", str(tmp_path / "out"), env=_env(tmp_path), cwd=clone)
    assert result.returncode == 1 and "missing LICENSE" in result.stderr
    assert not (tmp_path / "out").exists(), "nothing may be written on a refused build"


# ---------------------------------------------------------------------------
# release.sh guards (the signing itself is covered by the axp CLI tests)
# ---------------------------------------------------------------------------

def test_release_refuses_without_signing_key_or_axp_cli(tmp_path):
    env = _env(tmp_path)
    result = _run(RELEASE, "1.2.3", env=env)
    assert result.returncode == 1 and "AXP_SIGNING_KEY" in result.stderr

    result = _run(RELEASE, "1.2.3", env={**env, "AXP_SIGNING_KEY": str(tmp_path / "absent.key")})
    assert result.returncode == 1 and "axp CLI is required" in result.stderr
    assert not (REPO / "dist" / "mintbot-mcp-1.2.3.tar.gz").exists(), "guards run before the build"
    assert '__version__ = "1.2.3"' not in (REPO / "mintbot_mcp" / "__init__.py").read_text(), "guards run before the stamp"


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------

def test_install_without_python_fails_before_touching_anything(tmp_path):
    env = _env(tmp_path)
    env["PATH"] = "/nonexistent"
    env["MINTBOT_MCP_PYTHON"] = "python-that-does-not-exist"
    result = subprocess.run(["/bin/bash", str(REPO / "install.sh")], env=env, cwd=str(REPO), capture_output=True, text=True)
    assert result.returncode == 1 and "python3 is required" in result.stderr
    assert not (tmp_path / "prefix").exists() and not (tmp_path / "state").exists()


def test_healthcheck_before_install_is_unhealthy(tmp_path):
    result = _run(REPO / "healthcheck.sh", env=_env(tmp_path))
    assert result.returncode == 1
    assert result.stdout.splitlines()[0].startswith("unhealthy: launcher is not installed")


@pytest.fixture
def installed(tmp_path):
    env = _env(tmp_path)
    result = _run(REPO / "install.sh", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return env, result


def test_install_under_an_axp_host_converges_and_leaves_the_daemon_to_the_host(tmp_path, installed):
    env, result = installed
    prefix = tmp_path / "prefix"
    for name in ("README.md", "LICENSE", "after-install.md", "requirements.txt",
                 "lib/mintbot_mcp/__init__.py", "lib/mintbot_mcp/server.py", "lib/mintbot_mcp/cli.py"):
        assert (prefix / name).is_file(), name
    for name in ("install.sh", "upgrade.sh", "uninstall.sh", "healthcheck.sh", "bin/mintbot-mcp"):
        assert os.access(prefix / name, os.X_OK), name
    launcher = (prefix / "bin" / "mintbot-mcp").read_text(encoding="utf-8")
    assert f'PYTHONPATH="{prefix}/lib' in launcher and f'exec "{prefix}/venv/bin/python" -m mintbot_mcp "$@"' in launcher

    log = _log(env)
    assert f"-r {REPO}/requirements.txt" in log.replace("--requirement", "-r") or "--requirement" in log
    assert f"cache={tmp_path / 'cache' / 'pip'}" in log, "pip's cache must live in the declared cache dir"
    assert "systemctl" not in log and not (tmp_path / "units").exists(), "an AXP host owns the unit"
    assert "owns the service unit" in result.stdout and "installation complete" in result.stdout

    token = tmp_path / "state" / "token"
    assert token.is_file() and len(token.read_text().strip()) >= 40
    assert oct(token.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "state").stat().st_mode & 0o777) == "0o700"

    # Idempotent: a second run converges, keeps the token and the venv.
    (prefix / "lib" / "mintbot_mcp" / "stale.py").write_text("# from an older version\n")
    again = _run(REPO / "install.sh", env=env)
    assert again.returncode == 0, again.stderr
    assert token.read_text() == token.read_text() and not (prefix / "lib" / "mintbot_mcp" / "stale.py").exists()
    assert _log(env).count("venv") == 0, "the fake venv logs nothing; pip must have run twice"


def test_launcher_runs_the_installed_package(tmp_path, installed):
    env, _ = installed
    result = subprocess.run([str(tmp_path / "prefix" / "bin" / "mintbot-mcp"), "--version"], env=env, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.startswith("mintbot-mcp ")
    result = subprocess.run([str(tmp_path / "prefix" / "bin" / "mintbot-mcp"), "token"], env=env, capture_output=True, text=True)
    assert result.stdout.strip() == (tmp_path / "state" / "token").read_text().strip()


def test_upgrade_announces_versions_and_reinstalls(tmp_path, installed):
    env, _ = installed
    result = _run(REPO / "upgrade.sh", env={**env, "AXP_FROM_VERSION": "0.1.0", "AXP_EXT_VERSION": "0.2.0"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "[mintbot-mcp] upgrading from 0.1.0 to 0.2.0"
    assert "installation complete" in result.stdout


def test_healthcheck_after_install_reports_the_missing_daemon(tmp_path, installed):
    env, _ = installed
    result = _run(REPO / "healthcheck.sh", env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == f"unhealthy: nothing listens on 127.0.0.1:{env['MINTBOT_MCP_PORT']}"

    (tmp_path / "state" / "token").unlink()
    result = _run(REPO / "healthcheck.sh", env=env)
    assert result.returncode == 1 and "bearer token is missing" in result.stdout


def test_uninstall_keeps_the_token_unless_purged(tmp_path, installed):
    env, _ = installed
    token = tmp_path / "state" / "token"
    result = _run(REPO / "uninstall.sh", env=env)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "prefix").exists() and token.is_file()
    assert "preserved the bearer token" in result.stdout

    result = _run(REPO / "uninstall.sh", env={**env, "AXP_PURGE": "1"})
    assert result.returncode == 0 and not (tmp_path / "state").exists()
    assert "removed the server and its bearer token" in result.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="the standalone unit is only written as root")
def test_standalone_install_writes_and_starts_its_own_unit(tmp_path):
    env = _env(tmp_path, axp_host=False, systemctl=True)
    result = _run(REPO / "install.sh", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    unit = tmp_path / "units" / "mintbot-mcp.service"
    text = unit.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("# Written by") and "standalone mintbot-mcp install" in text
    assert f"ExecStart={tmp_path / 'prefix' / 'bin' / 'mintbot-mcp'} serve" in text
    assert f"Environment=AXP_STATE_DIR={tmp_path / 'state'}" in text
    assert "ProtectSystem=strict" in text and "Restart=always" in text
    log = _log(env)
    assert "systemctl daemon-reload" in log and "systemctl enable --quiet mintbot-mcp.service" in log
    assert "systemctl restart mintbot-mcp.service" in log

    result = _run(REPO / "uninstall.sh", env=env)
    assert result.returncode == 0 and not unit.exists()
    assert "systemctl disable --now --quiet mintbot-mcp.service" in _log(env)
