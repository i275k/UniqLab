# Building and developing UniqLab

## Requirements

* Python 3.12 (64-bit)
* The pinned packages in `requirements.txt` (runtime) and `requirements-dev.txt` (build/test)
* macOS packaging: Xcode command line tools (`codesign`, `hdiutil`)
* Windows packaging: Inno Setup 6.7.4 (`ISCC.exe`)
* Linux packaging: Ubuntu 22.04 (or another glibc 2.35 system), `curl`, `sha256sum`;
  `appimagetool` 1.9.1 and the type2 AppImage runtime are downloaded and verified by the script

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Project layout

```
uniqlab/
  app.py               user interface (PySide6)
  dependencies.py      component manager: detection, pinned downloads, SHA-256, safe extraction
  media_metadata.py    metadata plans, ExifTool/FFmpeg writing and verification
  metadata_presets.py  supported fields and evidence-based presets (texts are i18n keys)
  photo_processing.py  Pillow photo pipeline
  platform_support.py  paths, processes, fonts, reduced motion per OS
  selftest.py          --self-test checks used by CI on the packaged app
  settings.py          settings.json in the per-user data folder
  i18n/                catalogs (en, zh-CN, pt-BR, es, ru) and plural rules
  assets/              app icon, OFL fonts for Windows/Linux
packaging/             PyInstaller spec and per-OS packaging scripts
tools/                 icon renderer, privacy scanner, self-test report printer, Qt notices generator
tests/                 pytest suite
```

## Tests

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

Tests marked `tools` use real FFmpeg/ExifTool and are skipped when they are missing.
Install them with the app's own manager first (into a throwaway folder if you like):

```bash
export UNIQLAB_DATA_DIR="$PWD/.uniqlab-data"
python -m uniqlab.dependencies install
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

Useful environment variables:

| Variable | Effect |
|---|---|
| `UNIQLAB_DATA_DIR` | use another data folder (settings, profiles, downloaded tools) |
| `UNIQLAB_FONT_PROFILE=bundled` | preview the Windows/Linux fonts on macOS (`apple` forces SF Pro) |
| `UNIQLAB_REDUCE_MOTION=1` | disable animations |

## Translations

All user-facing text lives in `uniqlab/i18n/locales/<locale>.json` (English is the
reference). Keys are dotted paths; a plural entry is an object of CLDR categories selected
by the `n` parameter (`one/other` for en, es, pt-BR; `one/few/many/other` for ru; `other`
for zh-CN). `tests/test_i18n.py` checks that every catalog has every key, that placeholders
match English and that plural forms are complete. The brand name, the English subtitle
“VIDEO & IMAGE PROCESSING” and the author credit are intentionally not translated.

## Icons

```bash
python tools/make_icon.py
```

renders `uniqlab/assets/app_icon.png`, `packaging/macos/UniqLab.icns`,
`packaging/windows/UniqLab.ico` and `packaging/linux/uniqlab.png` without metadata.

## Packaging

| Platform | Command (repository root, virtualenv active) | Output |
|---|---|---|
| macOS arm64 | `packaging/macos/build_dmg.sh` | `dist/UniqLab-macos-arm64.dmg` |
| Windows x64 | `powershell -File packaging\windows\build_installer.ps1` | `dist\UniqLab-windows-x64.exe` |
| Linux x86_64 | `packaging/linux/build_appimage.sh` | `dist/UniqLab-linux-x64.AppImage` |

All three use the same PyInstaller one-folder spec (`packaging/uniqlab.spec`), so the Qt
libraries stay separate, replaceable files (LGPL). No FFmpeg/ExifTool binaries are
included. macOS builds are signed ad hoc (`codesign -s -`); nothing is notarized.

### Publishing safely

Build public packages **only in GitHub Actions**. A local build embeds paths from the build
machine: for example, a Homebrew Python installed in a home folder puts that home path
(and thus the user name) into `Python.framework` and `libcrypto`. Before sharing any
package, run the privacy scanner on its contents — CI does this automatically:

```bash
python tools/privacy_check.py dist/UniqLab --forbid "$(whoami)"
```

### Packaged-app self-test

Every package can check itself without a display:

```bash
QT_QPA_PLATFORM=offscreen UNIQLAB_DATA_DIR=/tmp/uniqlab-selftest \
  UniqLab.app/Contents/MacOS/UniqLab --self-test report.json --install-tools
python tools/print_selftest.py report.json
```

It verifies the bundled resources and license files, all five catalogs, the main window and
the Settings/Metadata pages, and Pillow photo processing with Unicode file names. Language
switching is checked against each catalog after the window's own background component
detection has finished — first on a clean system (FFmpeg/ExifTool installed on the machine are
hidden from detection, so every runner starts without components) and, with `--install-tools`,
again after the pinned FFmpeg/ExifTool have been downloaded into `UNIQLAB_DATA_DIR`. It then
processes a video and a photo with several metadata presets while network access is blocked
inside the process. Use an empty `UNIQLAB_DATA_DIR`. The option is not shown in the UI.

### Release workflow

1. Update `uniqlab/__init__.py` (`__version__`), `pyproject.toml` and the release entry in
   `packaging/linux/io.github.i275k.uniqlab.appdata.xml`.
2. Run the **CI** workflow (automatic on pushes and pull requests once the repository is
   public; manual — Actions → CI → Run workflow — while it is private, to save Actions
   minutes). It runs the tests on macOS, Windows and Linux, including real FFmpeg/ExifTool
   installed through the app's own dependency manager.
3. Start **Actions → Build installers → Run workflow**. It builds the DMG, the Windows
   installer and the AppImage, installs/mounts each one, checks the CPU architecture and
   signature, runs the packaged self-test above, scans the package contents for personal
   data and uploads the packages together with `SHA256SUMS.txt` as workflow artifacts
   (per-platform artifacts are kept 3 days, the combined set 7 days).
4. Optionally tick “create draft release”: a **draft** release is created with the packages.
   Nothing is ever published automatically — review the draft and publish it by hand.

Approximate cost of one full run on GitHub-hosted runners (private repositories consume
the free minutes; macOS minutes count 10×, Windows 2×): CI ≈ 6 min per OS, Build installers
≈ 10–15 min per OS. Public repositories use standard runners for free.

### Third-party notices

`licenses/` holds the license texts shipped in every package. `licenses/Qt-third-party-notices.txt`
is generated from the official qtbase source archive of the bundled Qt version with
`tools/make_qt_notices.py` (see the script header); regenerate it when PySide6 is updated.
On Linux, `packaging/linux/build_appimage.sh` additionally writes
`licenses/system-libraries/` into the package: the list of shared libraries PyInstaller copied
from the build system, with each Ubuntu package's copyright file. Qt's GTK3 platform theme is
left out of the Linux build (see `packaging/uniqlab.spec`), so the GTK stack is not bundled.

Workflow hardening: actions are pinned to full commit SHAs, the default token is read-only
(`contents: write` only in the opt-in draft job), checkouts do not persist credentials,
and every downloaded build tool (Inno Setup, appimagetool, AppImage runtime) is pinned and
verified with SHA-256.

### Git identity

Commits in this repository should use the public identity only:

```bash
git config user.name "onyx.x (i275k)"
git config user.email "<your GitHub noreply address>"
```

GitHub shows the noreply address under **Settings → Emails** (“Keep my email addresses
private”).

## Updating pinned components

`uniqlab/dependencies.py` holds the pinned FFmpeg/ExifTool downloads per platform. When
updating, take the version, URL and SHA-256 from the publisher's own checksum files
(martin-riedl.de `.sha256` files, FFmpeg-Builds `checksums.sha256`, ExifTool
`checksums.txt`), run `python -m uniqlab.dependencies install --managed` on each platform
and the `tools` tests.
