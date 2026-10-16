"""Real lifecycle scripts, isolated paths and fake venv/pip; never host services."""
import os
import shutil
import tarfile

from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_release_tooling import REPO, _env, _run


def environment(tmp_path):
    env = _env(tmp_path, systemctl=True)
    fake = tmp_path / "bin/python3"
    fake.write_text(f'''#!{sys.executable}
import os, pathlib, sys
if sys.argv[1:3] == ["-m", "venv"]:
    root = pathlib.Path(sys.argv[3])
    (root / "bin").mkdir(parents=True)
    (root / "bin/python").write_text('#!/bin/bash\\nexec {sys.executable} "$@"\\n')
    (root / "bin/pip").write_text('#!/bin/bash\\nprintf new > "' + str(root / 'dependencies') + '"\\n[ "${{FAIL_PIP:-0}}" != 1 ]\\n')
    # A console script with a real absolute venv shebang must survive activation.
    (root / "bin/console").write_text('#!' + str(root / 'bin/python') + '\\nprint("runtime-ok")\\n')
    for f in (root / "bin").iterdir():
        f.chmod(0o755)
    sys.exit(0)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
    return env


def launch(env, command="--version"):
    return subprocess.run([str(Path(env["AXP_PREFIX"]) / "bin/mintbot-mcp"), command],
                          env=env, cwd=env["HOME"], capture_output=True, text=True)


def test_failed_pip_never_modifies_the_active_runtime(tmp_path):
    env = environment(tmp_path)
    assert _run(REPO / "install.sh", env=env).returncode == 0
    prefix = Path(env["AXP_PREFIX"])
    runtime = (prefix / "venv").resolve()
    (runtime / "dependencies").write_text("old")
    before = launch(env).stdout
    token = (Path(env["AXP_STATE_DIR"]) / "token").read_bytes()
    result = _run(REPO / "upgrade.sh", env={**env, "FAIL_PIP": "1"})
    assert result.returncode != 0
    assert (runtime / "dependencies").read_text() == "old", "pip modified the active dependencies"
    assert launch(env).returncode == 0 and launch(env).stdout == before
    assert (Path(env["AXP_STATE_DIR"]) / "token").read_bytes() == token


def candidate(tmp_path):
    source = tmp_path / "candidate"
    shutil.copytree(REPO, source, ignore=shutil.ignore_patterns(
        ".git", ".venv", ".test-tmp", "__pycache__", ".pytest_cache"))
    return source


def test_candidate_healthcheck_must_pass_before_activation(tmp_path):
    env = environment(tmp_path)
    assert _run(REPO / "install.sh", env=env).returncode == 0
    prefix = Path(env["AXP_PREFIX"])
    previous = (prefix / "current").resolve()
    source = candidate(tmp_path)
    (source / "mintbot_mcp/server.py").write_text('raise RuntimeError("broken candidate dependencies")\n')
    result = _run(source / "upgrade.sh", env=env)
    assert result.returncode != 0, "installer activated a package whose server cannot import"
    assert (prefix / "current").resolve() == previous
    assert launch(env).returncode == 0


@pytest.mark.parametrize("failure", ["pip", "copy", "healthcheck", "activation"])
@pytest.mark.parametrize("legacy", [False, True], ids=["versioned", "legacy"])
def test_failed_upgrade_restores_every_entrypoint_and_removes_candidate(tmp_path, failure, legacy):
    env = environment(tmp_path)
    if legacy:
        legacy_install(env)
    else:
        assert _run(REPO / "install.sh", env=env).returncode == 0
    prefix = Path(env["AXP_PREFIX"])
    previous = (prefix / "current").resolve() if not legacy else None
    before = launch(env).stdout
    hook_before = (prefix / "healthcheck.sh").read_bytes()
    token = launch(env, "token").stdout
    data = Path(env["AXP_STATE_DIR"]) / "user-data"
    data.write_bytes(b"do not touch")
    source = candidate(tmp_path)
    if failure == "pip":
        env["FAIL_PIP"] = "1"
    elif failure == "copy":
        wrapper = tmp_path / "bin/install"
        wrapper.write_text('#!/bin/bash\nfor arg in "$@"; do\n  [[ "$arg" != */server.py ]] || exit 41\ndone\nexec /usr/bin/install "$@"\n')
        wrapper.chmod(0o755)
    elif failure == "healthcheck":
        (source / "healthcheck.sh").write_text('#!/bin/bash\nexit 42\n')
    else:
        wrapper = tmp_path / "bin/mv"
        wrapper.write_text('#!/bin/bash\nif [[ "${*: -1}" == */healthcheck.sh ]] && [ ! -e "${HOME}/failed" ]; then\n touch "${HOME}/failed"\n exit 43\nfi\nexec /usr/bin/mv "$@"\n')
        wrapper.chmod(0o755)
    result = _run(source / "upgrade.sh", env=env)
    assert result.returncode != 0, result.stdout + result.stderr
    if legacy:
        assert not (prefix / "current").exists()
    else:
        assert (prefix / "current").resolve() == previous
    assert launch(env).returncode == 0 and launch(env).stdout == before
    assert (prefix / "healthcheck.sh").read_bytes() == hook_before
    health = _run(prefix / "healthcheck.sh", "--offline", env=env)
    assert health.returncode == 0, health.stdout + health.stderr
    assert launch(env, "token").stdout == token
    assert data.read_bytes() == b"do not touch"
    assert set((prefix / "releases").iterdir()) == (set() if legacy else {previous})
    assert not list(prefix.glob(".*.next"))
    assert not list(prefix.glob(".transaction.*"))
    if Path(env["FAKE_LOG"]).exists():
        assert "systemctl" not in Path(env["FAKE_LOG"]).read_text()


def legacy_install(env):
    """Pre-transaction layout, with an absolute interpreter path and real code."""
    prefix = Path(env["AXP_PREFIX"])
    (prefix / "bin").mkdir(parents=True)
    subprocess.run(["python3", "-m", "venv", str(prefix / "venv")], env=env, check=True)
    shutil.copytree(REPO / "mintbot_mcp", prefix / "lib/mintbot_mcp")
    (prefix / "lib/mintbot_mcp/__init__.py").write_text('__version__ = "legacy"\n')
    for name in ("install.sh", "upgrade.sh", "healthcheck.sh", "uninstall.sh", "README.md", "LICENSE", "requirements.txt", "after-install.md"):
        shutil.copy2(REPO / name, prefix / name)
    launcher = prefix / "bin/mintbot-mcp"
    launcher.write_text(f'#!/bin/bash\nexport PYTHONPATH="{prefix}/lib"\nexec "{prefix}/venv/bin/python" -m mintbot_mcp "$@"\n')
    launcher.chmod(0o755)
    assert launch(env).stdout.strip() == "mintbot-mcp legacy"
    assert launch(env, "token").returncode == 0
    return prefix


def test_legacy_migration_and_installed_hooks_keep_new_code_and_shebangs(tmp_path):
    env = environment(tmp_path)
    prefix = legacy_install(env)
    old_python = (prefix / "venv/bin/python").read_bytes()
    token = launch(env, "token").stdout
    source = candidate(tmp_path)
    (source / "mintbot_mcp/__init__.py").write_text('__version__ = "next"\n')
    result = _run(source / "upgrade.sh", env=env)
    assert result.returncode == 0, result.stderr
    assert launch(env).stdout.strip() == "mintbot-mcp next"
    # Root hooks must resolve their versioned source, not copy legacy lib again.
    result = _run(prefix / "upgrade.sh", env=env)
    assert result.returncode == 0, result.stderr
    assert launch(env).stdout.strip() == "mintbot-mcp next"
    assert (prefix / "venv/bin/python").read_bytes() == old_python
    assert launch(env, "token").stdout == token
    runtime = (prefix / "current/venv").resolve()
    console = subprocess.run([str(runtime / "bin/console")], env=env, capture_output=True, text=True)
    assert console.returncode == 0 and console.stdout.strip() == "runtime-ok"
    assert (runtime / "bin/console").read_text().splitlines()[0] == f"#!{runtime}/bin/python"


def test_launcher_never_imports_the_callers_checkout(tmp_path):
    env = environment(tmp_path)
    source = candidate(tmp_path)
    (source / "mintbot_mcp/__init__.py").write_text('__version__ = "next"\n')
    result = _run(source / "install.sh", env=env)
    assert result.returncode == 0, result.stderr
    result = _run(Path(env["AXP_PREFIX"]) / "bin/mintbot-mcp", "--version", env=env, cwd=REPO)
    assert result.stdout.strip() == "mintbot-mcp next"


def standalone_environment(tmp_path):
    env = environment(tmp_path)
    env.pop("AXP_HOST")
    wrapper = tmp_path / "bin/systemctl"
    wrapper.write_text('''#!/bin/bash
printf 'systemctl %s\\n' "$*" >> "${FAKE_LOG}"
case "$1" in
  is-active) [ "${MOCK_ACTIVE:-0}" = 1 ] ;;
  is-enabled) [ "${WAS_ENABLED:-0}" = 1 ] ;;
  restart)
    if [ "${FAIL_RESTART:-0}" = 1 ] && [ ! -e "${HOME}/restart-failed" ]; then
      touch "${HOME}/restart-failed"
      exit 44
    fi ;;
esac
''')
    wrapper.chmod(0o755)
    return env


@pytest.mark.skipif(os.geteuid() != 0 or not Path("/run/systemd/system").is_dir(), reason="standalone systemd branch")
def test_standalone_upgrade_does_not_enable_or_start_a_disabled_service(tmp_path):
    env = standalone_environment(tmp_path)
    assert _run(REPO / "install.sh", env=env).returncode == 0
    Path(env["FAKE_LOG"]).write_text("")
    result = _run(REPO / "upgrade.sh", env=env)
    assert result.returncode == 0, result.stderr
    log = Path(env["FAKE_LOG"]).read_text()
    assert "systemctl enable " not in log and "systemctl restart " not in log


@pytest.mark.skipif(os.geteuid() != 0 or not Path("/run/systemd/system").is_dir(), reason="standalone systemd branch")
def test_service_failure_restores_unit_and_previous_release(tmp_path):
    env = standalone_environment(tmp_path)
    assert _run(REPO / "install.sh", env=env).returncode == 0
    prefix = Path(env["AXP_PREFIX"])
    previous = (prefix / "current").resolve()
    unit = Path(env["MINTBOT_MCP_UNIT_DIR"]) / "mintbot-mcp.service"
    unit.write_text(unit.read_text() + "# old unit marker\n")
    unit_before = unit.read_bytes()
    Path(env["FAKE_LOG"]).write_text("")
    result = _run(REPO / "upgrade.sh", env={**env, "MOCK_ACTIVE": "1", "WAS_ENABLED": "1", "FAIL_RESTART": "1"})
    assert result.returncode != 0
    assert (prefix / "current").resolve() == previous
    assert unit.read_bytes() == unit_before
    assert launch(env).returncode == 0
    assert Path(env["FAKE_LOG"]).read_text().count("systemctl restart ") == 2


def test_release_archive_is_self_contained_for_install_and_upgrade(tmp_path):
    env = environment(tmp_path)
    result = _run(REPO / "scripts/build-artifact.sh", "test", str(tmp_path / "dist"), env=env)
    assert result.returncode == 0, result.stderr
    source = tmp_path / "unpacked"
    with tarfile.open(tmp_path / "dist/mintbot-mcp-test.tar.gz") as archive:
        archive.extractall(source, filter="data")
    result = _run(source / "install.sh", env=env, cwd=env["HOME"])
    assert result.returncode == 0, result.stderr
    previous = (Path(env["AXP_PREFIX"]) / "current").resolve()
    result = _run(source / "upgrade.sh", env=env, cwd=env["HOME"])
    assert result.returncode == 0, result.stderr
    assert (Path(env["AXP_PREFIX"]) / "current").resolve() != previous
    assert launch(env).returncode == 0


def test_host_disabled_state_is_untouched_on_success(tmp_path):
    env = environment(tmp_path)
    disabled = tmp_path / "host-record.json"
    disabled.write_text('{"enabled": false, "policy": "off"}\n')
    before = disabled.read_bytes()
    assert _run(REPO / "install.sh", env=env).returncode == 0
    assert _run(REPO / "upgrade.sh", env=env).returncode == 0
    assert disabled.read_bytes() == before
    assert not Path(env["FAKE_LOG"]).exists(), "host owns enablement and service restart"
