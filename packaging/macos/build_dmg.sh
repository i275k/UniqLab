#!/usr/bin/env bash
# Build UniqLab.app (PyInstaller) and pack it into an ad-hoc signed DMG for Apple silicon.
# No paid Developer ID certificate and no notarization: see docs/INSTALL.md for the first launch.
# Usage (repository root, inside the build virtualenv):  packaging/macos/build_dmg.sh
#
# The app is built and signed in a temporary folder: folders synced by iCloud/File Provider
# (such as ~/Documents) re-add Finder attributes that codesign rejects.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
VERSION="$(python -c 'import uniqlab; print(uniqlab.__version__)')"
ARCH="$(uname -m)"
if [[ "$ARCH" != "arm64" ]]; then
  echo "This script builds the Apple silicon (arm64) release; current machine: $ARCH" >&2
  exit 1
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/uniqlab-build.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
pyinstaller --noconfirm --clean --distpath "$WORK/dist" --workpath "$WORK/build" packaging/uniqlab.spec

APP="$WORK/dist/UniqLab.app"
# Remove extended attributes and re-sign the whole bundle ad hoc (no identity, no timestamp server).
xattr -cr "$APP"
codesign --force --deep --sign - --timestamp=none "$APP"
codesign --verify --deep --strict --verbose=1 "$APP"

STAGE="$WORK/dmg"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/UniqLab.app"
ln -s /Applications "$STAGE/Applications"
NAME="UniqLab-macos-arm64.dmg"  # stable name for releases/latest/download links
hdiutil create -volname "UniqLab ${VERSION}" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$WORK/$NAME"
codesign --force --sign - --timestamp=none "$WORK/$NAME"

mkdir -p dist
cp "$WORK/$NAME" "dist/$NAME"
( cd dist && shasum -a 256 "$NAME" > "$NAME.sha256" )
echo "Built dist/$NAME"
