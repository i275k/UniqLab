"""Generate licenses/Qt-third-party-notices.txt from the official qtbase source archive.

The PySide6 wheels ship Qt binaries without the notices of the third-party code compiled into
Qt (FreeType, HarfBuzz, libpng, PCRE2, …). This script collects every ``qt_attribution.json`` of the
qtbase source release that matches the bundled Qt version, together with the license texts it points
to, so the notices travel with every UniqLab package.

    curl -LO https://download.qt.io/official_releases/qt/6.11/6.11.3/submodules/qtbase-everywhere-src-6.11.3.tar.xz
    # compare with the .sha256 file published next to it, then:
    tar -xJf qtbase-everywhere-src-6.11.3.tar.xz
    python tools/make_qt_notices.py qtbase-everywhere-src-6.11.3
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Build tooling, tests and other platforms: not part of the Qt libraries shipped with UniqLab.
SKIP = ("tests/", "util/", "cmake/", "src/testlib/", "src/testinternal/", "src/3rdparty/android",
        "src/3rdparty/gradle", "src/3rdparty/wasm")


def entries(source: Path):
    for path in sorted(source.rglob("qt_attribution.json")):
        rel = path.relative_to(source).as_posix()
        if rel.startswith(SKIP):
            continue
        data = json.loads(path.read_text(encoding="utf-8"), strict=False)  # some files contain raw tabs
        for item in data if isinstance(data, list) else [data]:
            if item.get("QtUsage", "").lower().startswith("not used"):
                continue
            yield path.parent, item


def main(source_dir: str) -> None:
    source = Path(source_dir)
    version = source.name.rsplit("-", 1)[-1]
    out = [
        f"Third-party components compiled into Qt {version} (qtbase)",
        "=" * 64,
        "",
        "UniqLab bundles the Qt libraries from PySide6-Essentials, unmodified (LGPL-3.0, see",
        "LGPL-3.0.txt). Qt itself contains the following third-party code. This file was generated",
        f"by tools/make_qt_notices.py from qtbase-everywhere-src-{version}.tar.xz (download.qt.io).",
        "Depending on the platform, some components are not compiled in or the system copy is used.",
        "",
    ]
    seen_texts: dict[str, str] = {}
    blocks = []
    for folder, item in entries(source):
        name = item.get("Name") or item.get("Id")
        header = [name, "-" * len(name)]
        for key in ("Description", "Version", "License", "Homepage", "DownloadLocation"):
            if item.get(key):
                header.append(f"{key}: {item[key]}")
        copyright_text = item.get("Copyright")
        if isinstance(copyright_text, list):
            copyright_text = "\n".join(copyright_text)
        if copyright_text:
            header.append("Copyright:\n  " + copyright_text.strip().replace("\n", "\n  "))
        files = item.get("LicenseFiles") or ([item["LicenseFile"]] if item.get("LicenseFile") else [])
        for name_file in files:
            path = (folder / name_file).resolve()
            if not path.is_file():
                header.append(f"(license file {name_file} not found in the source archive)")
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text in seen_texts.values():
                ref = next(k for k, v in seen_texts.items() if v == text)
                header.append(f"License text: identical to [{ref}] above.")
            else:
                label = f"{name}: {path.name}"
                seen_texts[label] = text
                header.append(f"License text [{label}]:\n\n" + text)
        blocks.append("\n".join(header))
    out.append(f"{len(blocks)} entries.\n")
    out.append("\n\n\n".join(blocks))
    target = ROOT / "licenses" / "Qt-third-party-notices.txt"
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {target.relative_to(ROOT)} ({len(blocks)} entries)")


if __name__ == "__main__":
    main(sys.argv[1])
