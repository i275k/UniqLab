# UniqLab

**Video & image processing** — a small desktop app that makes subtle, local changes to
videos and photos and can write evidence-based device and editor metadata presets.

by [onyx.x (i275k)](https://github.com/i275k) · MIT License

* **Video:** random 0.05–0.15 s trims at both ends, light noise and a 1–2 % brightness or
  contrast shift, a center crop to a platform aspect ratio (9:16, 16:9, 1:1, 4:5, …) or a
  random 2–10 % crop. Encoding with libx264/libx265 on the CPU.
* **Photo:** JPEG, PNG and WebP; three strengths (light / medium / strong) of crop, tone
  and noise changes that keep the look of the picture.
* **Metadata presets:** iPhone, Samsung Galaxy, Premiere Pro, DaVinci Resolve and more.
  Every preset lists its sources and its limitations; values are written with ExifTool and
  FFmpeg and then read back and verified. GPS/location data is never written.
* **Five languages:** English, 简体中文, Português (Brasil), Español, Русский — detected
  from the system on first launch and switchable at any time without a restart.
* **Private by design:** no accounts, no telemetry, no analytics, no tracking, no cloud.
  The network is used only when you ask UniqLab to download components or check for updates.

## Download and install

Release packages are built by GitHub Actions from this repository:

| Platform | Package |
|---|---|
| macOS 13+ on Apple silicon (arm64) | `UniqLab-<version>-macos-arm64.dmg` |
| Windows 10/11 x64 | `UniqLab-<version>-windows-x64.exe` (per-user, no admin rights) |
| Linux x86_64 (glibc 2.35+) | `UniqLab-<version>-linux-x64.AppImage` |

Verify the download against `SHA256SUMS.txt`. The packages are **not** signed with paid
certificates, so macOS and Windows show a warning on first launch — see
[docs/INSTALL.md](docs/INSTALL.md) for the exact steps.

## Components: FFmpeg and ExifTool

UniqLab does not ship FFmpeg, FFprobe or ExifTool. Open **Settings** (click the status
pill in the header) → **Components**:

* tools already installed on the system are detected automatically;
* **Download & Install All** fetches pinned official builds (FFmpeg with libx264/libx265,
  ExifTool) into UniqLab's own data folder, verifies their SHA-256 checksums and needs no
  administrator rights;
* **Choose file…** points UniqLab to any compatible copy you already have;
* **Check for updates** looks for newer builds only when you click it.

Video mode needs FFmpeg; photo mode works without it; metadata presets need ExifTool.
On macOS and Linux ExifTool runs with the system Perl (preinstalled on macOS; on Linux
install the `perl` package if it is missing). After installation everything works offline.

## Run from source

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uniqlab
```

Headless component management (useful on servers and CI):

```bash
python -m uniqlab.dependencies status
python -m uniqlab.dependencies install
```

See [docs/BUILDING.md](docs/BUILDING.md) for tests, packaging and the release workflow.

## Where UniqLab keeps its data

| System | Folder |
|---|---|
| macOS | `~/Library/Application Support/UniqLab` |
| Windows | `%LOCALAPPDATA%\UniqLab` |
| Linux | `$XDG_DATA_HOME/UniqLab` (usually `~/.local/share/UniqLab`) |

It contains `settings.json` (language, theme, chosen tool paths), `metadata_profiles.json`
(your own presets) and `tools/` (components downloaded by UniqLab). Delete the folder to
reset everything. Set `UNIQLAB_DATA_DIR` to use another folder.

## License

UniqLab is MIT-licensed — see [LICENSE](LICENSE). Bundled third-party software (Qt/PySide6
under LGPL-3.0, Pillow, certifi, fonts) and the separately downloaded tools are described in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Product names used in metadata presets are trademarks of their owners; UniqLab is not
affiliated with or endorsed by them. Use the app only with media you have the right to edit.
