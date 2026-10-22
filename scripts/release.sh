#!/usr/bin/env bash
# Cut a signed AXP release: stamp the version into the Python package, build
# the deterministic artifact, then let `axp release` set the manifest version,
# fill every sha256, stamp published_at / valid_until and sign the manifest
# with the publisher key.
#
#   AXP_SIGNING_KEY=/path/to/mintbot.ai-mintbot-mcp.key scripts/release.sh 0.1.0
#
# The freshness window (release.valid_until, SPEC section 7.4) is 180 days by
# default - generous on purpose: hosts warn once it lapses, so it is a promise
# to re-release within that time. AXP_VALID_DAYS overrides (0 omits the field).
#
# The signing key must be listed in agent-extension-keys.json (the SPEC 8.5
# publisher key directory served at https://mintbot.ai/.well-known/); a release
# signed by an unlisted key is refused here before it can reach a host.
#
# Afterwards: review the diff, commit, tag v<version>, and attach BOTH
# dist/mintbot-mcp-<version>.tar.gz and agent-extension.json to the GitHub
# release (SPEC section 7.1: hosts on the `github` update source read the
# manifest from the release asset named agent-extension.json). A stable
# release is never a GitHub pre-release: it becomes the repository's "latest",
# which is what a bare repository URL installs.
#
# scripts/release-dev.sh runs this script on a scratch export of one commit
# (RELEASE_TREE), on the dev channel (AXP_CHANNEL), with the dev key
# (AXP_SIGNING_KEY_ID) checked against the DEV key directory (AXP_KEYDIR). By
# default the release is cut in this checkout, on the channel and with the
# signing key the manifest names, checked against agent-extension-keys.json.
set -euo pipefail

VERSION="${1:?usage: release.sh <version>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TREE="${RELEASE_TREE:-${ROOT}}"
KEY="${AXP_SIGNING_KEY:?set AXP_SIGNING_KEY to the publisher private key (never commit it)}"
command -v axp >/dev/null 2>&1 || { echo "release: the axp CLI is required (pip install -e ../agent-extension)" >&2; exit 1; }
KEYDIR="${AXP_KEYDIR:-${TREE}/agent-extension-keys.json}"

# Signing with a key other than the manifest's own (scripts/release-dev.sh
# signs with the dev key): its key directory entry becomes the signing block.
# `axp release` refuses when the private key does not match that entry.
if [ -n "${AXP_SIGNING_KEY_ID:-}" ]; then
  python3 - "${TREE}/agent-extension.json" "${KEYDIR}" "${AXP_SIGNING_KEY_ID}" <<'PY'
import json
import pathlib
import sys

path, keydir, key_id = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
keys = json.loads(keydir.read_text(encoding="utf-8"))["keys"]
entry = next((key for key in keys if key.get("key_id") == key_id), None)
if entry is None:
    sys.exit(f"release: {key_id} is not listed in {keydir}")
data = json.loads(path.read_text(encoding="utf-8"))
data["signing"] = {"public_key": entry["public_key"], "key_id": key_id, "next_key": None}
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
fi

# One version everywhere: the manifest (below), the package and pyproject.
python3 - "${TREE}" "${VERSION}" <<'PY'
import pathlib
import re
import sys

root, version = pathlib.Path(sys.argv[1]), sys.argv[2]
for path, pattern in (
    (root / "mintbot_mcp" / "__init__.py", r'^__version__ = "[^"]+"$'),
    (root / "pyproject.toml", r'^version = "[^"]+"$'),
):
    text = path.read_text(encoding="utf-8")
    key = pattern.split(" ")[0].lstrip("^")
    new, count = re.subn(pattern, f'{key} = "{version}"', text, count=1, flags=re.M)
    if count != 1:
        sys.exit(f"release: no version line in {path}")
    path.write_text(new, encoding="utf-8")
PY

# The release notes live on the GitHub release of this very version; `axp
# release` refreshes the artifact URLs but not this one.
python3 - "${TREE}/agent-extension.json" "${VERSION}" <<'PY'
import json
import pathlib
import sys

path, version = pathlib.Path(sys.argv[1]), sys.argv[2]
data = json.loads(path.read_text(encoding="utf-8"))
data.setdefault("release", {})["changelog"] = f"https://github.com/mintbot-ai/mintbot-mcp/releases/tag/v{version}"
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY

CHANNEL="${AXP_CHANNEL:-$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["release"]["channel"])' "${TREE}/agent-extension.json")}"
ARTIFACT="$("${TREE}/scripts/build-artifact.sh" "${VERSION}" | cut -d" " -f3)"
axp release "${TREE}/agent-extension.json" \
  --version "${VERSION}" \
  --artifact "${ARTIFACT}" \
  --channel "${CHANNEL}" \
  --valid-days "${AXP_VALID_DAYS:-180}" \
  --key "${KEY}"
axp verify "${TREE}/agent-extension.json" --keydir "${KEYDIR}"
if [ "${TREE}" = "${ROOT}" ]; then
  echo
  echo "next: git commit -am 'release v${VERSION}' && git tag v${VERSION} && git push --tags"
  echo "      gh release create v${VERSION} ${ARTIFACT} ${ROOT}/agent-extension.json --notes-file CHANGELOG.md"
fi
