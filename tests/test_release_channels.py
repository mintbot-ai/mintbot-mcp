"""Publishing on two channels: `release.sh` on a tree it is handed and with
the key it is named, `release-dev.sh` end to end, the dev key directory and
the workflow that publishes every push to main.

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
STABLE_KEYDIR = REPO / "agent-extension-keys.json"
DEV_KEYDIR = REPO / "agent-extension-keys.dev.json"
DEV_KEY_ID = "mintbot.ai-mintbot-mcp-dev-2026"
WORKFLOW = REPO / ".github" / "workflows" / "dev-build.yml"
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
  "release create")
    echo "$3" >> "${FAKE_RELEASES}"
    for asset in "$@"; do
      case "${asset}" in *agent-extension.json) cp "${asset}" "${FAKE_RELEASES}.manifest.json" ;; esac
    done ;;
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


def _keys(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["keys"]


def _dev_signing() -> dict:
    dev = next(key for key in _keys(DEV_KEYDIR) if key["key_id"] == DEV_KEY_ID)
    return {"public_key": dev["public_key"], "key_id": DEV_KEY_ID, "next_key": None}


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
    signing = json.loads(manifest.read_text(encoding="utf-8"))["signing"]
    assert signing["key_id"] == "mintbot.ai-mintbot-mcp-2026", "without AXP_SIGNING_KEY_ID the manifest's key signs"
    assert f"axp verify {manifest} --keydir {clone}/agent-extension-keys.json" in _log(publisher)
    assert "next:" not in result.stdout, "the commit and tag hints are for a release cut in the checkout"


def test_release_signs_with_the_key_directory_entry_it_is_named(publisher, clone):
    env = {**publisher, "RELEASE_TREE": str(clone), "AXP_SIGNING_KEY_ID": DEV_KEY_ID,
           "AXP_KEYDIR": str(clone / "agent-extension-keys.dev.json")}
    result = _run(RELEASE, "9.9.9", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((clone / "agent-extension.json").read_text(encoding="utf-8"))
    assert manifest["signing"] == _dev_signing()
    assert (f"axp verify {clone}/agent-extension.json --keydir {clone}/agent-extension-keys.dev.json"
            in _log(publisher)), "the named key is checked against the directory it came from"


def test_release_refuses_a_key_the_directory_does_not_list(publisher, clone):
    env = {**publisher, "RELEASE_TREE": str(clone), "AXP_SIGNING_KEY_ID": DEV_KEY_ID}
    result = _run(RELEASE, "9.9.9", env=env)
    assert result.returncode == 1
    assert f"{DEV_KEY_ID} is not listed in {clone}/agent-extension-keys.json" in result.stderr
    assert '__version__ = "9.9.9"' not in (clone / "mintbot_mcp" / "__init__.py").read_text(encoding="utf-8")
    assert _log(publisher) == "", "nothing is built or signed"


def test_the_dev_key_directory_is_the_stable_one_plus_the_dev_key():
    """The directory mintbot.dev serves to DEV agents (its copy lives in the
    mintbot repository): every stable key, and the dev key mintbot.ai never lists."""
    stable, dev = _keys(STABLE_KEYDIR), _keys(DEV_KEYDIR)
    assert json.loads(DEV_KEYDIR.read_text(encoding="utf-8"))["publisher"] == "mintbot.ai"
    assert dev[:len(stable)] == stable
    added = dev[len(stable):]
    assert [(key["key_id"], key["extensions"]) for key in added] == [(DEV_KEY_ID, ["mintbot-mcp"])]
    assert added[0]["public_key"] not in {key["public_key"] for key in stable}


def test_the_workflow_publishes_main_with_the_dev_key_alone():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "push:\n    branches: [main]" in text and "release:\n    types: [published]" in text
    assert "!github.event.release.prerelease" in text, "its own dev builds start no further build"
    assert re.findall(r"secrets\.\w+", text) == ["secrets.AXP_DEV_SIGNING_KEY"], "the stable key is never a secret"
    assert re.search(r"agent-extension@[0-9a-f]{40}\"", text), "the axp CLI is pinned to a commit"
    assert "permissions:\n  contents: write\n" in text and "scripts/release-dev.sh" in text


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
    assert re.search(r"^axp verify \S+/agent-extension\.json --keydir \S+/agent-extension-keys\.dev\.json$", log, re.M)
    uploaded = json.loads((tmp_path / "releases.manifest.json").read_text(encoding="utf-8"))
    assert uploaded["signing"] == _dev_signing(), "a dev build carries the dev key, never the stable one"
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
