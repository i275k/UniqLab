"""Dependency manager for the external tools UniqLab drives: FFmpeg and ExifTool.

UniqLab does not ship these binaries. They are found on the system, chosen manually, or
downloaded on request from upstream / established build providers into the per-user data
folder (no administrator rights). Every download is pinned to a version and verified by
SHA-256 before anything is extracted; extraction refuses absolute paths, ".." segments and
links. Once installed, all processing works offline.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import ssl
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from uniqlab import APP_NAME, __version__
from uniqlab.platform_support import IS_MACOS, IS_WINDOWS, app_data_dir, executable_name, platform_key, run, which

TOOLS = ("ffmpeg", "exiftool")
USER_AGENT = f"{APP_NAME}/{__version__} (+https://github.com/i275k)"
REQUIRED_ENCODERS = ("libx264", "libx265")
SUPPORTED_PLATFORMS = ("macos-arm64", "windows-x64", "linux-x64")


# ------------------------------------------------------------------------- pinned downloads


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str
    archive: str  # "zip" | "tar.xz" | "tar.gz"
    mirrors: tuple = ()


@dataclass(frozen=True)
class Release:
    tool: str
    version: str
    provider: str
    artifacts: tuple
    license_note: str
    homepage: str


_MR = "https://ffmpeg.martin-riedl.de/download/macos/arm64/1783011502_8.1.2"
_BTBN = "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-11-26-12-56"
_SF = "https://downloads.sourceforge.net/project/exiftool"

PINNED: dict[str, dict[str, Release]] = {
    "ffmpeg": {
        "macos-arm64": Release(
            "ffmpeg", "8.1.2", "martin-riedl.de (static macOS arm64 build)",
            (
                Artifact(f"{_MR}/ffmpeg.zip", "ef1aa60006c7b77ce170c1608c08d8e4ba1c30c5746f2ac986ded932d0ac2c3c", "zip"),
                Artifact(f"{_MR}/ffprobe.zip", "c39787f4af7a3932502d2d48db6f6feaaa836b48a73ef78c32cc3285df61dfaf", "zip"),
            ),
            "GPL-3.0 build (libx264, libx265)", "https://ffmpeg.martin-riedl.de/",
        ),
        "windows-x64": Release(
            "ffmpeg", "8.1.3-41-ga8029b6528", "FFmpeg-Builds (BtbN), win64 GPL",
            (Artifact(f"{_BTBN}/ffmpeg-n8.1.3-41-ga8029b6528-win64-gpl-8.1.zip",
                      "dbbd7275987024a34bfad96b3c3cc148a47bea604ff7100705d8f14ab1cabfb8", "zip"),),
            "GPL-3.0 build (libx264, libx265)", "https://github.com/FFmpeg-Builds/FFmpeg-Builds",
        ),
        "linux-x64": Release(
            "ffmpeg", "8.1.3-41-ga8029b6528", "FFmpeg-Builds (BtbN), linux64 GPL",
            (Artifact(f"{_BTBN}/ffmpeg-n8.1.3-41-ga8029b6528-linux64-gpl-8.1.tar.xz",
                      "f7520f98cfc98b6fe147716d709b8ff9f4d6d184ac1b203745cb6987f2dc62c3", "tar.xz"),),
            "GPL-3.0 build (libx264, libx265)", "https://github.com/FFmpeg-Builds/FFmpeg-Builds",
        ),
    },
    "exiftool": {
        "macos-arm64": Release(
            "exiftool", "13.64", "exiftool.org (Perl distribution)",
            (Artifact(f"{_SF}/Image-ExifTool-13.64.tar.gz",
                      "dcb9f6be1f6705ee826a92d1522e3fec1d0b3749cdc061b03c46873d5fe204fc", "tar.gz",
                      ("https://sourceforge.net/projects/exiftool/files/Image-ExifTool-13.64.tar.gz/download",)),),
            "Perl Artistic License / GPL", "https://exiftool.org/",
        ),
        "linux-x64": Release(
            "exiftool", "13.64", "exiftool.org (Perl distribution)",
            (Artifact(f"{_SF}/Image-ExifTool-13.64.tar.gz",
                      "dcb9f6be1f6705ee826a92d1522e3fec1d0b3749cdc061b03c46873d5fe204fc", "tar.gz",
                      ("https://sourceforge.net/projects/exiftool/files/Image-ExifTool-13.64.tar.gz/download",)),),
            "Perl Artistic License / GPL", "https://exiftool.org/",
        ),
        "windows-x64": Release(
            "exiftool", "13.64", "exiftool.org (Windows executable with bundled Perl)",
            (Artifact(f"{_SF}/exiftool-13.64_64.zip",
                      "72c29c44d1e860eebcaefa71692d979af9858fe9a6d7d791a5e7f1dac77804af", "zip",
                      ("https://sourceforge.net/projects/exiftool/files/exiftool-13.64_64.zip/download",)),),
            "Perl Artistic License / GPL", "https://exiftool.org/",
        ),
    },
}


def pinned_release(tool: str, key: str | None = None) -> Release | None:
    return PINNED.get(tool, {}).get(key or platform_key())


# ------------------------------------------------------------------------- status


@dataclass
class ToolStatus:
    tool: str
    state: str = "missing"  # ok | missing | incompatible | broken
    version: str = ""
    path: str = ""
    source: str = ""  # manual | managed | system
    detail: str = ""  # i18n key
    detail_params: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def tools_dir(data_dir: Path | None = None) -> Path:
    return (data_dir or app_data_dir()) / "tools"


def perl_path() -> str | None:
    if IS_WINDOWS:
        return None
    for candidate in ("/usr/bin/perl", which("perl")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _managed_record(tool: str, data_dir: Path | None) -> dict:
    try:
        return json.loads((tools_dir(data_dir) / tool / "current.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def exiftool_command(path: str) -> list[str]:
    """How to launch ExifTool at ``path``: the Perl script needs Perl, the Windows .exe does not."""
    if path.lower().endswith(".exe"):
        return [path]
    perl = perl_path()
    return [perl, path] if perl else [path]


def check_ffmpeg(path: str) -> ToolStatus:
    status = ToolStatus("ffmpeg", path=path)
    try:
        result = run([path, "-hide_banner", "-version"], capture_output=True, text=True, timeout=20)
        encoders = run([path, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        status.state, status.detail, status.detail_params = "broken", "deps.detail.not_runnable", {"error": str(exc)[:120]}
        return status
    if result.returncode != 0:
        status.state, status.detail = "broken", "deps.detail.not_runnable"
        status.detail_params = {"error": (result.stderr or "").strip()[:120]}
        return status
    match = re.search(r"ffmpeg version (\S+)", result.stdout or "")
    # Some builds append their homepage ("8.1.2-https://…"); keep the version only.
    status.version = re.sub(r"-https?://\S*$", "", match.group(1)) if match else "?"
    missing = [name for name in REQUIRED_ENCODERS if not re.search(rf"\b{name}\b", encoders.stdout or "")]
    if missing:
        status.state, status.detail, status.detail_params = "incompatible", "deps.detail.missing_encoders", {"names": ", ".join(missing)}
        return status
    status.state = "ok"
    return status


def check_exiftool(path: str) -> ToolStatus:
    status = ToolStatus("exiftool", path=path)
    if not path.lower().endswith(".exe") and not IS_WINDOWS and perl_path() is None:
        status.state, status.detail = "broken", "deps.detail.perl_missing"
        return status
    try:
        result = run(exiftool_command(path) + ["-ver"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        status.state, status.detail, status.detail_params = "broken", "deps.detail.not_runnable", {"error": str(exc)[:120]}
        return status
    version = (result.stdout or "").strip()
    if result.returncode != 0 or not re.fullmatch(r"\d+\.\d+", version):
        status.state, status.detail = "broken", "deps.detail.not_runnable"
        status.detail_params = {"error": (result.stderr or version).strip()[:120]}
        return status
    status.version, status.state = version, "ok"
    return status


def _system_candidates(tool: str) -> list[str]:
    names = ["ffmpeg"] if tool == "ffmpeg" else ["exiftool"]
    found = [which(name) for name in names]
    if IS_MACOS:
        for prefix in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
            found.append(str(Path(prefix) / names[0]))
    return [path for path in dict.fromkeys(found) if path and Path(path).exists()]


def detect(tool: str, manual_path: str | None = None, data_dir: Path | None = None) -> ToolStatus:
    """Find a working tool: manual choice → UniqLab-managed install → system PATH."""
    checker = check_ffmpeg if tool == "ffmpeg" else check_exiftool
    best: ToolStatus | None = None
    candidates: list[tuple[str, str]] = []
    if manual_path:
        candidates.append(("manual", manual_path))
    record = _managed_record(tool, data_dir)
    if record.get("path"):
        candidates.append(("managed", record["path"]))
    candidates += [("system", path) for path in _system_candidates(tool)]
    for source, path in candidates:
        if not Path(path).exists():
            continue
        status = checker(path)
        status.source = source
        if source == "managed" and record.get("version"):
            status.detail_params.setdefault("installed", record["version"])
        if status.ok:
            return status
        best = best or status
    return best or ToolStatus(tool, detail="deps.detail.not_found")


# ------------------------------------------------------------------------- downloading


class DownloadError(Exception):
    def __init__(self, key: str, /, **params):
        super().__init__(key)
        self.key = key
        self.params = params


ProgressCallback = Callable[[str, int, int], None]  # (stage, done_bytes, total_bytes)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover - certifi is a declared dependency
        return ssl.create_default_context()


def _open(url: str, offset: int = 0, timeout: int = 30):
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout, context=_ssl_context())


def fetch_text(url: str, timeout: int = 30) -> str:
    with _open(url, timeout=timeout) as response:
        return response.read(4 * 1024 * 1024).decode("utf-8", "replace")


def download(url: str, target: Path, expected_sha256: str, progress: ProgressCallback | None = None,
             cancel: threading.Event | None = None, retries: int = 4) -> Path:
    """Download with resume and retries, then verify SHA-256 (the file is deleted on mismatch)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    attempt = 0
    while True:
        if cancel and cancel.is_set():
            raise DownloadError("deps.error.cancelled")
        offset = partial.stat().st_size if partial.exists() else 0
        try:
            with _open(url, offset) as response:
                status = getattr(response, "status", 200)
                if offset and status != 206:  # server ignored Range: start over
                    offset = 0
                length = response.headers.get("Content-Length")
                total = offset + int(length) if length and length.isdigit() else 0
                with partial.open("ab" if offset else "wb") as handle:
                    done = offset
                    while True:
                        if cancel and cancel.is_set():
                            raise DownloadError("deps.error.cancelled")
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress("download", done, total)
            if total and partial.stat().st_size < total:
                raise urllib.error.URLError("incomplete download")
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise DownloadError("deps.error.not_found", url=url) from exc
            if exc.code == 416:  # range past the end: the .part is complete or stale
                break
            attempt += 1
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            attempt += 1
            if attempt > retries:
                raise DownloadError("deps.error.network", error=str(getattr(exc, "reason", exc))[:160]) from exc
        if attempt > retries:
            raise DownloadError("deps.error.network", error="retries exhausted")
        time.sleep(min(8, 2 ** attempt))
    if progress:
        progress("verify", 0, 0)
    actual = sha256_file(partial)
    if actual.lower() != expected_sha256.lower():
        partial.unlink(missing_ok=True)
        raise DownloadError("deps.error.checksum", expected=expected_sha256[:16], actual=actual[:16])
    partial.replace(target)
    return target


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------------- safe extraction


def _safe_member(name: str) -> PurePosixPath | None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized):
        return None
    if any(part in ("..", "") for part in path.parts if part != "."):
        return None
    return path


def safe_extract(archive: Path, kind: str, destination: Path, wanted: Callable[[PurePosixPath], PurePosixPath | None]) -> list[Path]:
    """Extract only regular files selected by ``wanted`` (which returns the relative output path)."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    written: list[Path] = []

    def place(member: PurePosixPath, reader) -> None:
        relative = wanted(member)
        if relative is None:
            return
        target = (destination / Path(*relative.parts)).resolve()
        if root not in target.parents:
            raise DownloadError("deps.error.unsafe_archive", name=str(member))
        target.parent.mkdir(parents=True, exist_ok=True)
        with reader() as source, target.open("wb") as sink:
            shutil.copyfileobj(source, sink)
        written.append(target)

    if kind == "zip":
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                member = _safe_member(info.filename)
                is_link = stat.S_ISLNK(info.external_attr >> 16)
                if info.is_dir() or is_link:
                    continue
                if member is None:
                    raise DownloadError("deps.error.unsafe_archive", name=info.filename)
                place(member, lambda info=info: bundle.open(info))
    else:
        mode = "r:xz" if kind == "tar.xz" else "r:gz"
        with tarfile.open(archive, mode) as bundle:
            for info in bundle.getmembers():
                if not info.isfile():  # skips links, devices and directories
                    continue
                member = _safe_member(info.name)
                if member is None:
                    raise DownloadError("deps.error.unsafe_archive", name=info.name)
                place(member, lambda info=info: bundle.extractfile(info))
    return written


def _make_executable(path: Path) -> None:
    if not IS_WINDOWS:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ------------------------------------------------------------------------- installation


def _ffmpeg_selector(member: PurePosixPath) -> PurePosixPath | None:
    if member.name in (executable_name("ffmpeg"), executable_name("ffprobe")):
        return PurePosixPath(member.name)
    return None


def _exiftool_selector(member: PurePosixPath) -> PurePosixPath | None:
    parts = member.parts
    if len(parts) < 2:
        return None
    inner = PurePosixPath(*parts[1:])
    if IS_WINDOWS:
        if inner.name == "exiftool(-k).exe" and len(inner.parts) == 1:
            return PurePosixPath("exiftool.exe")  # documented rename for command-line use
        if inner.parts[0] == "exiftool_files":
            return inner
        return None
    if inner == PurePosixPath("exiftool") or inner.parts[0] == "lib":
        return inner
    return None


def install(release: Release, data_dir: Path | None = None, progress: ProgressCallback | None = None,
            cancel: threading.Event | None = None) -> ToolStatus:
    tool = release.tool
    base = tools_dir(data_dir) / tool
    downloads = tools_dir(data_dir) / ".downloads"
    staging = Path(tempfile.mkdtemp(prefix=f"{tool}-", dir=_ensure(base)))
    try:
        for index, artifact in enumerate(release.artifacts):
            name = f"{tool}-{release.version}-{index}.{artifact.archive}"
            archive = downloads / name
            urls = (artifact.url, *artifact.mirrors)
            last_error: DownloadError | None = None
            for url in urls:
                try:
                    download(url, archive, artifact.sha256, progress, cancel)
                    last_error = None
                    break
                except DownloadError as exc:
                    if exc.key in ("deps.error.cancelled", "deps.error.checksum"):
                        raise
                    last_error = exc
            if last_error:
                raise last_error
            if progress:
                progress("extract", 0, 0)
            safe_extract(archive, artifact.archive, staging, _ffmpeg_selector if tool == "ffmpeg" else _exiftool_selector)
            archive.unlink(missing_ok=True)
        main = staging / (executable_name("ffmpeg") if tool == "ffmpeg" else ("exiftool.exe" if IS_WINDOWS else "exiftool"))
        if not main.exists():
            raise DownloadError("deps.error.layout", name=main.name)
        for path in staging.iterdir():
            if path.is_file():
                _make_executable(path)
        target = base / _safe_version(release.version)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        main = target / main.name
        if progress:
            progress("check", 0, 0)
        status = check_ffmpeg(str(main)) if tool == "ffmpeg" else check_exiftool(str(main))
        if not status.ok:
            shutil.rmtree(target, ignore_errors=True)
            raise DownloadError("deps.error.incompatible", detail=status.detail)
        status.source = "managed"
        record = {"version": release.version, "path": str(main), "provider": release.provider,
                  "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        (base / "current.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        _remove_old_versions(base, keep=target.name)
        return status
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_version(version: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", version)


def _remove_old_versions(base: Path, keep: str) -> None:
    for child in base.iterdir():
        if child.is_dir() and child.name != keep and not child.name.startswith("."):
            shutil.rmtree(child, ignore_errors=True)


# ------------------------------------------------------------------------- update checks


def latest_release(tool: str, key: str | None = None) -> Release | None:
    """Newest upstream build (user-initiated). Hashes come from the provider's checksum files."""
    key = key or platform_key()
    pinned = pinned_release(tool, key)
    if pinned is None:
        return None
    if tool == "exiftool":
        version = fetch_text("https://exiftool.org/ver.txt").strip()
        if not re.fullmatch(r"\d+\.\d+", version):
            return None
        sums = fetch_text("https://exiftool.org/checksums.txt")
        name = f"exiftool-{version}_64.zip" if key.startswith("windows") else f"Image-ExifTool-{version}.tar.gz"
        match = re.search(rf"SHA2-256\({re.escape(name)}\)=\s*([0-9a-f]{{64}})", sums)
        if not match:
            return None
        kind = "zip" if name.endswith(".zip") else "tar.gz"
        artifact = Artifact(f"{_SF}/{name}", match.group(1), kind,
                            (f"https://sourceforge.net/projects/exiftool/files/{name}/download",))
        return Release(tool, version, pinned.provider, (artifact,), pinned.license_note, pinned.homepage)
    if key == "macos-arm64":
        page = fetch_text("https://ffmpeg.martin-riedl.de/info/history/macos/arm64/release")
        match = re.search(r"/info/detail/macos/arm64/(\d+_([0-9][0-9.]*))\"", page)
        if not match:
            return None
        build, version = match.group(1), match.group(2)
        base = f"https://ffmpeg.martin-riedl.de/download/macos/arm64/{build}"
        artifacts = []
        for name in ("ffmpeg.zip", "ffprobe.zip"):
            digest = fetch_text(f"{base}/{name}.sha256").split()[0]
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                return None
            artifacts.append(Artifact(f"{base}/{name}", digest, "zip"))
        return Release(tool, version, pinned.provider, tuple(artifacts), pinned.license_note, pinned.homepage)
    # Windows / Linux: newest daily build of the same FFmpeg release branch.
    releases = json.loads(fetch_text("https://api.github.com/repos/FFmpeg-Builds/FFmpeg-Builds/releases?per_page=3"))
    flavour = "win64-gpl" if key.startswith("windows") else "linux64-gpl"
    branch = pinned.artifacts[0].url.rsplit("-", 1)[-1].split(".zip")[0].split(".tar")[0]  # e.g. "8.1"
    for entry in releases:
        if not str(entry.get("tag_name", "")).startswith("autobuild-"):
            continue
        assets = {asset["name"]: asset["browser_download_url"] for asset in entry.get("assets", [])}
        sums_url = assets.get("checksums.sha256")
        pattern = re.compile(rf"ffmpeg-n({re.escape(branch)}[^-]*-\d+-g[0-9a-f]+)-{flavour}-{re.escape(branch)}\.(zip|tar\.xz)$")
        for name, url in assets.items():
            match = pattern.match(name)
            if match and sums_url:
                sums = fetch_text(sums_url)
                digest = re.search(rf"([0-9a-f]{{64}})\s+\*?{re.escape(name)}", sums)
                if digest:
                    artifact = Artifact(url, digest.group(1), match.group(2))
                    return Release(tool, match.group(1), pinned.provider, (artifact,), pinned.license_note, pinned.homepage)
    return None


def version_key(version: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:4])


def is_newer(candidate: str, current: str) -> bool:
    return bool(current) and version_key(candidate) > version_key(current)


# ------------------------------------------------------------------------- command line


def _cli(argv: list[str] | None = None) -> int:
    """Headless use (CI, servers):  python -m uniqlab.dependencies status|install [--tool NAME] [--managed]"""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m uniqlab.dependencies", description="UniqLab component manager")
    parser.add_argument("command", choices=("status", "install"))
    parser.add_argument("--tool", choices=TOOLS, action="append", help="limit to one tool (repeatable)")
    parser.add_argument("--managed", action="store_true",
                        help="install UniqLab's pinned builds even if another working copy is found")
    args = parser.parse_args(argv)
    tools = args.tool or list(TOOLS)
    print(f"platform: {platform_key()}  data: {tools_dir()}")
    failed = False
    for tool in tools:
        status = detect(tool)
        if args.command == "install" and (not status.ok or (args.managed and status.source != "managed")):
            release = pinned_release(tool)
            if release is None:
                print(f"{tool}: no pinned download for {platform_key()}")
                failed = True
                continue
            last = [""]

            def report(stage: str, done: int, total: int, last=last) -> None:
                # One line per stage and per 10 % of the download.
                step = f"{stage} {done * 10 // total * 10}%" if stage == "download" and total else stage
                if step != last[0]:
                    last[0] = step
                    print(f"{tool}: {step}", flush=True)

            try:
                status = install(release, progress=report)
            except DownloadError as exc:
                print(f"{tool}: failed — {exc.key} {exc.params}")
                failed = True
                continue
        print(f"{tool}: {status.state} {status.version} ({status.source or '-'}) {status.path}")
        failed = failed or not status.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_cli())
