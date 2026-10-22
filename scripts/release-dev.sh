#!/usr/bin/env bash
# Publish a dev build: a commit on main (the newest by default), signed on the
# `dev` channel (SPEC section 7.2) and published as a GitHub pre-release.
#
#   AXP_SIGNING_KEY=/path/to/mintbot.ai-mintbot-mcp.key scripts/release-dev.sh [<commit>]
#
# The version is the patch after the latest stable release plus a UTC
# timestamp, e.g. 0.1.4-dev.20261022170105: each dev build sorts above the one
# before it and below the stable release it leads to, so promoting main to a
# stable release needs no version juggling.
#
# A host follows the channel of the manifest it installed: an agent that
# installed a dev build receives dev builds, one that installed a stable
# release never sees them. Two rules keep it that way:
# - A stable release must be the repository's GitHub "latest". A bare
#   repository URL installs releases/latest and reads the full listing, dev
#   builds included, only when there is none; without one this script refuses.
# - Only the newest dev builds are kept. A host weighs the five newest versions
#   above its own (MAX_RELEASE_CANDIDATES in axp/repository.py), so a pile of
#   dev builds would hide the next stable release from every stable host.
#
# The working tree is never touched: the commit is exported to a scratch
# directory and stamped, built and signed there by scripts/release.sh.
set -euo pipefail

REPO="mintbot-ai/mintbot-mcp"
KEEP=2
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${AXP_SIGNING_KEY:?set AXP_SIGNING_KEY to the publisher private key (never commit it)}"
for tool in axp gh git; do
  command -v "${tool}" >/dev/null 2>&1 || { echo "release-dev: ${tool} is required" >&2; exit 1; }
done

git -C "${ROOT}" fetch --quiet --tags origin
COMMIT="$(git -C "${ROOT}" rev-parse --verify "${1:-origin/main}^{commit}")"
git -C "${ROOT}" merge-base --is-ancestor "${COMMIT}" origin/main \
  || { echo "release-dev: ${COMMIT} is not on origin/main" >&2; exit 1; }

STABLE="$(gh release view --repo "${REPO}" --json tagName --jq .tagName 2>/dev/null || true)"
if [[ ! "${STABLE}" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "release-dev: ${REPO} has no stable latest release; a bare repository install would pick a dev build" >&2
  exit 1
fi
VERSION="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.$((BASH_REMATCH[3] + 1))-dev.$(date -u +%Y%m%d%H%M%S)"

TREE="$(mktemp -d)"
trap 'rm -rf "${TREE}"' EXIT
git -C "${ROOT}" archive "${COMMIT}" | tar -x -C "${TREE}"
RELEASE_TREE="${TREE}" AXP_CHANNEL=dev bash "${ROOT}/scripts/release.sh" "${VERSION}"

gh release create "v${VERSION}" --repo "${REPO}" --target "${COMMIT}" --prerelease \
  --title "v${VERSION} (dev)" \
  --notes "Dev build of ${COMMIT:0:12} on main. Agents that installed a stable release never receive it." \
  "${TREE}/dist/mintbot-mcp-${VERSION}.tar.gz" "${TREE}/agent-extension.json"

# The timestamp grows with every build, whatever the base version.
gh release list --repo "${REPO}" --limit 100 --json tagName --jq '.[].tagName' \
  | { grep -E '^v[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]+$' || true; } \
  | sort -t. -k4,4nr \
  | tail -n "+$((KEEP + 1))" \
  | while read -r old; do
      gh release delete "${old}" --repo "${REPO}" --cleanup-tag --yes
    done

echo "published v${VERSION}: https://github.com/${REPO}/releases/download/v${VERSION}/agent-extension.json"
