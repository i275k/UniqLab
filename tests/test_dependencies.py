"""Dependency manager: manifest, safe extraction, verified/resumable downloads, detection."""

from __future__ import annotations

import hashlib
import http.server
import io
import re
import tarfile
import threading
import zipfile
from pathlib import PurePosixPath

import pytest

from uniqlab import dependencies as d


def test_manifest_covers_every_platform():
    for tool in d.TOOLS:
        for key in d.SUPPORTED_PLATFORMS:
            release = d.pinned_release(tool, key)
            assert release is not None, (tool, key)
            assert release.tool == tool and release.version
            for artifact in release.artifacts:
                assert artifact.url.startswith("https://"), artifact.url
                assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256), artifact.url
                assert artifact.archive in ("zip", "tar.xz", "tar.gz")
                for mirror in artifact.mirrors:
                    assert mirror.startswith("https://")


def test_ffmpeg_builds_are_gpl_with_x264_x265():
    # GPL builds are the ones that include libx264/libx265.
    for key in ("windows-x64", "linux-x64"):
        assert "-gpl-" in d.pinned_release("ffmpeg", key).artifacts[0].url
    assert d.REQUIRED_ENCODERS == ("libx264", "libx265")


@pytest.mark.parametrize(("a", "b", "newer"), [("8.1.3", "8.1.2", True), ("8.1.2", "8.1.2", False),
                                              ("13.64", "13.7", True), ("n8.1.3-41-gabc", "8.1.3", True),
                                              ("n8.1.3-41-gabc", "n8.1.3-41-gabc", False)])
def test_version_compare(a, b, newer):
    assert d.is_newer(a, b) is newer


def _zip_with(names: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in names.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _take_all(member: PurePosixPath) -> PurePosixPath:
    return PurePosixPath(member.name)


@pytest.mark.parametrize("evil", ["../escape.txt", "/abs/escape.txt", "C:/escape.txt", "a/../../escape.txt"])
def test_safe_extract_rejects_traversal_zip(tmp_path, evil):
    archive = tmp_path / "evil.zip"
    archive.write_bytes(_zip_with({"ok.txt": b"ok", evil: b"bad"}))
    with pytest.raises(d.DownloadError) as error:
        d.safe_extract(archive, "zip", tmp_path / "out", _take_all)
    assert error.value.key == "deps.error.unsafe_archive"
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_traversal_tar(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"bad"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(d.DownloadError):
        d.safe_extract(archive, "tar.gz", tmp_path / "out", _take_all)
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_skips_links(tmp_path):
    archive = tmp_path / "links.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        link = tarfile.TarInfo("bin/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("bin/tool")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    out = tmp_path / "out"
    d.safe_extract(archive, "tar.gz", out, _take_all)
    assert (out / "tool").exists()
    assert not (out / "link").exists()


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b"x" * 200_000
    fail_once = False

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        start = 0
        header = self.headers.get("Range")
        if header:
            start = int(header.split("=")[1].split("-")[0])
        body = self.payload[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if type(self).fail_once:
            type(self).fail_once = False
            self.wfile.write(body[: len(body) // 3])
            self.wfile.flush()
            self.connection.shutdown(2)
            return
        self.wfile.write(body)


@pytest.fixture()
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/file.bin"
    httpd.shutdown()


def test_download_verifies_checksum(tmp_path, server):
    good = hashlib.sha256(_Handler.payload).hexdigest()
    target = d.download(server, tmp_path / "file.bin", good, retries=1)
    assert target.read_bytes() == _Handler.payload


def test_download_deletes_on_mismatch(tmp_path, server):
    with pytest.raises(d.DownloadError) as error:
        d.download(server, tmp_path / "file.bin", "0" * 64, retries=1)
    assert error.value.key == "deps.error.checksum"
    assert not (tmp_path / "file.bin").exists()


def test_download_resumes_after_interruption(tmp_path, server, monkeypatch):
    monkeypatch.setattr(d.time, "sleep", lambda _s: None)
    _Handler.fail_once = True
    good = hashlib.sha256(_Handler.payload).hexdigest()
    stages = []
    target = d.download(server, tmp_path / "file.bin", good, progress=lambda s, done, total: stages.append(s), retries=3)
    assert target.read_bytes() == _Handler.payload
    assert "download" in stages and "verify" in stages


def test_download_can_be_cancelled(tmp_path, server):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(d.DownloadError) as error:
        d.download(server, tmp_path / "file.bin", "0" * 64, cancel=cancel)
    assert error.value.key == "deps.error.cancelled"


def test_detect_reports_missing_and_broken(tmp_path):
    fake = tmp_path / "ffmpeg-not-really"
    fake.write_text("not a program")
    status = d.detect("ffmpeg", manual_path=str(fake), data_dir=tmp_path)
    if status.source == "manual":
        assert status.state == "broken"
    assert d.detect("ffmpeg", manual_path=str(tmp_path / "missing"), data_dir=tmp_path).tool == "ffmpeg"


def test_exiftool_command_uses_perl_for_script(tmp_path):
    command = d.exiftool_command(str(tmp_path / "exiftool"))
    assert command[-1].endswith("exiftool")
    assert d.exiftool_command("C:/x/exiftool.exe") == ["C:/x/exiftool.exe"]


def test_user_agent_has_no_personal_data():
    assert d.USER_AGENT.startswith("UniqLab/")
    assert "@" not in d.USER_AGENT
