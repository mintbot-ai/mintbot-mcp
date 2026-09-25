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
# manifest from the release asset named agent-extension.json).
set -euo pipefail

VERSION="${1:?usage: release.sh <version>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="${AXP_SIGNING_KEY:?set AXP_SIGNING_KEY to the publisher private key (never commit it)}"
command -v axp >/dev/null 2>&1 || { echo "release: the axp CLI is required (pip install -e ../agent-extension)" >&2; exit 1; }

# One version everywhere: the manifest (below), the package and pyproject.
python3 - "${ROOT}" "${VERSION}" <<'PY'
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

ARTIFACT="$("${ROOT}/scripts/build-artifact.sh" "${VERSION}" | cut -d" " -f3)"
axp release "${ROOT}/agent-extension.json" \
  --version "${VERSION}" \
  --artifact "${ARTIFACT}" \
  --channel "$(python3 -c "import json; print(json.load(open('${ROOT}/agent-extension.json'))['release']['channel'])")" \
  --valid-days "${AXP_VALID_DAYS:-180}" \
  --key "${KEY}"
axp verify "${ROOT}/agent-extension.json" --keydir "${ROOT}/agent-extension-keys.json"
echo
echo "next: git commit -am 'release v${VERSION}' && git tag v${VERSION} && git push --tags"
echo "      gh release create v${VERSION} ${ARTIFACT} ${ROOT}/agent-extension.json --notes-file CHANGELOG.md"
