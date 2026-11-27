#!/usr/bin/env bash
# Build the UniqLab AppImage for Linux x86_64 (build on Ubuntu 22.04 for broad glibc compatibility).
# appimagetool and the AppImage runtime are pinned and verified with SHA-256.
# Usage (repository root, inside the build virtualenv):  packaging/linux/build_appimage.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
VERSION="$(python -c 'import uniqlab; print(uniqlab.__version__)')"

APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

TOOLS="packaging/.tools"
mkdir -p "$TOOLS"
fetch() {  # url sha256 target
  if [[ ! -f "$3" ]] || ! echo "$2  $3" | sha256sum -c --status; then
    curl -fL --retry 4 --retry-delay 3 -o "$3" "$1"
  fi
  echo "$2  $3" | sha256sum -c -
}
fetch "$APPIMAGETOOL_URL" "$APPIMAGETOOL_SHA256" "$TOOLS/appimagetool"
fetch "$RUNTIME_URL" "$RUNTIME_SHA256" "$TOOLS/runtime-x86_64"
chmod +x "$TOOLS/appimagetool"

rm -rf build dist
pyinstaller --noconfirm --clean packaging/uniqlab.spec

APPDIR="packaging/linux/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo" \
         "$APPDIR/usr/share/icons/hicolor/512x512/apps"
cp -R dist/UniqLab "$APPDIR/usr/lib/UniqLab"
cp packaging/linux/AppRun "$APPDIR/AppRun"
cp packaging/linux/uniqlab.desktop "$APPDIR/uniqlab.desktop"
cp packaging/linux/uniqlab.desktop "$APPDIR/usr/share/applications/uniqlab.desktop"
cp packaging/linux/io.github.i275k.uniqlab.appdata.xml "$APPDIR/usr/share/metainfo/"
cp packaging/linux/uniqlab.png "$APPDIR/uniqlab.png"
cp packaging/linux/uniqlab.png "$APPDIR/usr/share/icons/hicolor/512x512/apps/uniqlab.png"
ln -sf uniqlab.png "$APPDIR/.DirIcon"

OUT="dist/UniqLab-${VERSION}-linux-x64.AppImage"
# --appimage-extract-and-run: works on runners without FUSE.
ARCH=x86_64 "$TOOLS/appimagetool" --appimage-extract-and-run --no-appstream \
  --runtime-file "$TOOLS/runtime-x86_64" "$APPDIR" "$OUT"
chmod +x "$OUT"
( cd dist && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
echo "Built $OUT"
