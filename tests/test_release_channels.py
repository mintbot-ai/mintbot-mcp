"""Publishing on two channels: `release.sh` on a tree it is handed and
`release-dev.sh` end to end.

The scripts run for real against a throwaway git clone of the runtime tree,
with fake `axp`, `gh` and `git fetch` on PATH that only log: no test touches
the signing key, GitHub or a remote.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_release_tooling import RELEASE, REPO, _env, _log, _run

RELEASE_DEV = REPO / "scripts" / "release-dev.sh"
GIT_IDENTITY = ["-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]

_FAKE_AXP = """#!/bin/bash
echo "axp $*" >> "${FAKE_LOG}"
"""

# GitHub as the scripts see it: the latest stable release, the release list
# (which grows by every created release) and a log of every call.
_FAKE_GH = """#!/bin/bash
echo "gh $*" >> "${FAKE_LOG}"
case "$1 $2" in
  "release view") [ -n "${FAKE_LATEST:-}" ] || exit 1; echo "${FAKE_LATEST}" ;;
  "release list") cat "${FAKE_RELEASES}" ;;
  "release create") echo "$3" >> "${FAKE_RELEASES}" ;;
esac
"""

# The throwaway clone has no remote: fetch is the one git call that needs one.
_FAKE_GIT = """#!/bin/bash
[ "$3" = "fetch" ] && exit 0
exec __REAL__ "$@"
"""


@pytest.fixture
def publisher(tmp_path: Path) -> dict:
    env = _env(tmp_path)
    bin_dir = tmp_path / "bin"
    for name, script in (("axp", _FAKE_AXP), ("gh", _FAKE_GH),
                         ("git", _FAKE_GIT.replace("__REAL__", shutil.which("git") or "git"))):
        (bin_dir / name).write_text(script, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    releases = tmp_path / "releases"
    releases.write_text("v0.1.4-dev.20261020000000\nv0.1.3\nv0.1.4-dev.20261021000000\nv0.1.2\n", encoding="utf-8")
    (tmp_path / "scratch").mkdir()
    env.update(AXP_SIGNING_KEY=str(tmp_path / "publisher.key"), FAKE_LATEST="v0.1.3",
               FAKE_RELEASES=str(releases), TMPDIR=str(tmp_path / "scratch"))
    return env


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """The runtime tree as a fresh git repository whose origin/main is its HEAD."""
    clone = tmp_path / "clone"
    shutil.copytree(REPO, clone, ignore=shutil.ignore_patterns(
        ".git", ".venv", ".test-tmp", "dist", "__pycache__", "tests", "*.pyc", "*.egg-info"))
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "tree"],
                 ["update-ref", "refs/remotes/origin/main", "HEAD"]):
        subprocess.run(["git", "-C", str(clone), *GIT_IDENTITY, *args], check=True, capture_output=True)
    return clone


def _git(clone: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True, text=True).stdout


def test_release_dev_parses_and_fails_closed():
    assert subprocess.run(["bash", "-n", str(RELEASE_DEV)], capture_output=True).returncode == 0
    text = RELEASE_DEV.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
    assert RELEASE_DEV.stat().st_mode & 0o111, "release-dev.sh is not executable"


def test_release_signs_the_tree_it_is_handed_never_the_checkout(publisher, clone):
    result = _run(RELEASE, "9.9.9", env={**publisher, "RELEASE_TREE": str(clone)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert '__version__ = "9.9.9"' in (clone / "mintbot_mcp" / "__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "9.9.9"' not in (REPO / "mintbot_mcp" / "__init__.py").read_text(encoding="utf-8")
    assert (clone / "dist" / "mintbot-mcp-9.9.9.tar.gz").is_file()
    assert not (REPO / "dist" / "mintbot-mcp-9.9.9.tar.gz").exists()
    manifest = clone / "agent-extension.json"
    channel = json.loads(manifest.read_text(encoding="utf-8"))["release"]["channel"]
    assert (f"axp release {manifest} --version 9.9.9 --artifact {clone}/dist/mintbot-mcp-9.9.9.tar.gz "
            f"--channel {channel} ") in _log(publisher), "without AXP_CHANNEL the manifest names the channel"
    assert "next:" not in result.stdout, "the commit and tag hints are for a release cut in the checkout"


def test_release_dev_publishes_a_dev_build_and_keeps_the_newest_two(tmp_path, publisher, clone):
    commit = _git(clone, "rev-parse", "HEAD").strip()
    result = _run(clone / "scripts" / "release-dev.sh", env=publisher, cwd=clone)
    assert result.returncode == 0, result.stdout + result.stderr
    log = _log(publisher)

    created = re.search(r"^gh release create v(0\.1\.4-dev\.\d{14}) --repo mintbot-ai/mintbot-mcp "
                        r"--target (\w+) --prerelease ", log, re.M)
    assert created, log
    version, target = created.groups()
    assert target == commit, "the tag goes on the commit that was built"
    assert re.search(rf"^axp release \S+/agent-extension\.json --version {re.escape(version)} .* --channel dev ", log, re.M)
    assert f"published v{version}: " in result.stdout

    deleted = re.findall(r"^gh release delete (\S+) ", log, re.M)
    assert deleted == ["v0.1.4-dev.20261020000000"], "only dev builds beyond the newest two go, never a stable one"
    assert _git(clone, "status", "--porcelain") == "" and not (clone / "dist").exists(), "the checkout stays untouched"
    assert not any((tmp_path / "scratch").iterdir()), "the scratch export is removed"


@pytest.mark.parametrize("latest", ["", "v0.1.4-dev.20261021000000"], ids=["none", "pre-release"])
def test_release_dev_refuses_without_a_stable_latest_release(publisher, clone, latest):
    env = {**publisher, "FAKE_LATEST": latest}
    result = _run(clone / "scripts" / "release-dev.sh", env=env, cwd=clone)
    assert result.returncode == 1 and "has no stable latest release" in result.stderr
    assert "axp release" not in _log(env) and "gh release create" not in _log(env)


def test_release_dev_refuses_a_commit_that_is_not_on_main(publisher, clone):
    _git(clone, *GIT_IDENTITY, "commit", "-q", "--allow-empty", "-m", "local only")
    result = _run(clone / "scripts" / "release-dev.sh", "HEAD", env=publisher, cwd=clone)
    assert result.returncode == 1 and "is not on origin/main" in result.stderr
    assert "gh release" not in _log(publisher)


def test_release_dev_refuses_without_signing_key_or_axp_cli(tmp_path):
    env = _env(tmp_path)
    result = _run(RELEASE_DEV, env=env)
    assert result.returncode == 1 and "AXP_SIGNING_KEY" in result.stderr
    result = _run(RELEASE_DEV, env={**env, "AXP_SIGNING_KEY": str(tmp_path / "absent.key")})
    assert result.returncode == 1 and "axp is required" in result.stderr
