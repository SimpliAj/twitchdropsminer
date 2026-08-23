#!/usr/bin/env bash
set -euo pipefail

# release.sh - Cut a new TwitchDropsMiner release the way it's actually been done
# (30 times by hand): bump version, tag main, push, publish a GitHub Release.
#
# Usage: scripts/release.sh <version> <notes_file>
#
#   version:     New version, e.g. 1.3.36
#   notes_file:  Path to a Markdown file with the release body (## Added / ## Changed / ## Fixed, etc.)
#
# What it does:
#   1. Validates the repo is clean and on main, up to date with origin/main
#   2. Validates <version> is SemVer and greater than the current version
#   3. Updates src/version.py and pyproject.toml, commits, tags vX.Y.Z
#   4. Pushes the commit and tag to origin (never upstream)
#   5. Creates the GitHub Release from notes_file via `gh release create`
#
# docker-ghcr.yml then builds and pushes the Docker image automatically on push to main.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <version> <notes_file>" >&2
  echo "  e.g.: $0 1.3.36 /tmp/notes.md" >&2
  exit 1
fi

VERSION="$1"
NOTES_FILE="$2"

if [ ! -f "$NOTES_FILE" ]; then
  echo "Error: notes file '$NOTES_FILE' not found" >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: gh CLI is required" >&2
  exit 1
fi

CURRENT_VERSION=$(grep -oP '__version__ = "\K[^"]+' src/version.py)

echo "Current version: $CURRENT_VERSION"
echo "Target version:  $VERSION"

.github/scripts/validate_semver.sh "$VERSION" ">$CURRENT_VERSION" >/dev/null

if [ -n "$(git status --porcelain)" ]; then
  echo "Error: working tree is not clean, commit or stash changes first" >&2
  exit 1
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "main" ]; then
  echo "Error: must be on main (currently on '$BRANCH')" >&2
  exit 1
fi

git fetch origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "Error: local main is not up to date with origin/main" >&2
  exit 1
fi

TAG="v$VERSION"
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Error: tag '$TAG' already exists" >&2
  exit 1
fi

echo "Updating src/version.py and pyproject.toml..."
echo "__version__ = \"$VERSION\"" > src/version.py
sed -i "s/^version = \"[^\"]*\"\(.*\)/version = \"$VERSION\"\1/" pyproject.toml

git add src/version.py pyproject.toml
git commit -m "chore: bump version to $VERSION"
git tag "$TAG"

git push origin main
git push origin "$TAG"

echo "Creating GitHub Release $TAG..."
gh release create "$TAG" \
  --repo SimpliAj/twitchdropsminer \
  --title "$TAG" \
  --notes-file "$NOTES_FILE"

echo ""
echo "Done. $TAG pushed to main and released."
echo "docker-ghcr.yml will build and push the Docker image on this push to main."
