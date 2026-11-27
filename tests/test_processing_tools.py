"""End-to-end checks with real FFmpeg/ExifTool (skipped when the tools are not installed)."""

from __future__ import annotations

import subprocess

import pytest
from PIL import Image

from uniqlab.metadata_presets import builtin_profile

pytestmark = pytest.mark.tools


def _make_video(ffmpeg: str, path):
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def test_video_processing_neutral(tools, tmp_path):
    from uniqlab.app import probe_duration, run_ffmpeg

    source = _make_video(tools["ffmpeg"], tmp_path / "in.mp4")
    output = tmp_path / "out.mp4"
    run_ffmpeg(source, output, "9:16")
    assert output.stat().st_size > 1000
    assert 1.0 < probe_duration(output) < 2.0


@pytest.mark.parametrize("preset", ["builtin:video-iphone-12-pro", "builtin:video-premiere-2024", "builtin:video-galaxy-android14"])
def test_video_metadata_preset_verified(tools, tmp_path, preset):
    from uniqlab.app import run_ffmpeg
    from uniqlab.media_metadata import build_plan, ffmpeg_video_options, probe_fps, verify_output, video_container, write_video_metadata

    source = _make_video(tools["ffmpeg"], tmp_path / "in.mp4")
    profile = builtin_profile(preset)
    container = video_container(profile)
    output = tmp_path / f"out.{container}"
    plan = build_plan(profile, "video", container, source)
    run_ffmpeg(source, output, "16:9", encode_options=ffmpeg_video_options(profile, plan))
    plan = build_plan(profile, "video", container, source, fps=probe_fps(tools["ffmpeg"], output),
                      moment=(plan.moment, plan.moment_note))
    write_video_metadata(profile, plan, source, output)
    report = verify_output(plan, output, tools["ffmpeg"])
    assert report.ok, report.summary()


@pytest.mark.parametrize("preset", ["builtin:photo-iphone-15-pro", "builtin:photo-galaxy-s25-ultra"])
def test_photo_metadata_preset_verified(tools, tmp_path, preset):
    from uniqlab.media_metadata import build_plan, read_metadata, verify_output, write_photo_metadata
    from uniqlab.photo_processing import process_photo

    source = tmp_path / "in.jpg"
    Image.new("RGB", (400, 300), (40, 90, 160)).save(source, "JPEG")
    output = tmp_path / "out.jpg"
    info = process_photo(source, output, "medium")
    meta = read_metadata(source)
    profile = builtin_profile(preset)
    plan = build_plan(profile, "photo", info["format"], source, source_meta=meta)
    write_photo_metadata(profile, plan, source, output, meta)
    report = verify_output(plan, output, tools["ffmpeg"])
    assert report.ok, report.summary()
    written = read_metadata(output)
    assert not any("GPS" in key for key in written)


def test_neutral_photo_has_no_source_metadata(tools, tmp_path):
    from uniqlab.media_metadata import build_plan, read_metadata, write_photo_metadata
    from uniqlab.photo_processing import process_photo

    source = tmp_path / "in.jpg"
    image = Image.new("RGB", (200, 200))
    exif = Image.Exif()
    exif[0x013B] = "Original Artist"
    image.save(source, "JPEG", exif=exif.tobytes())
    output = tmp_path / "out.jpg"
    info = process_photo(source, output, "light")
    profile = builtin_profile("builtin:photo-none")
    meta = read_metadata(source)
    write_photo_metadata(profile, build_plan(profile, "photo", info["format"], source, source_meta=meta), source, output, meta)
    assert "Original Artist" not in str(read_metadata(output))


@pytest.fixture()
def processing_window(qapp, tmp_path, english):
    from uniqlab.app import UniqLabWindow
    from uniqlab.settings import AppSettings

    settings = AppSettings(tmp_path / "settings.json")
    settings.set("first_run_offer_done", True)
    win = UniqLabWindow(settings)
    yield win
    win.close()
    win.deleteLater()


@pytest.mark.parametrize("extension", ["png", "webp", "jpg"])
def test_window_photo_pipeline_with_preset(tools, tmp_path, processing_window, extension):
    """The full per-file photo path of the window, including formats where some fields are skipped."""
    source = tmp_path / f"in photo é.{extension}"
    Image.new("RGB", (480, 360), (200, 120, 40)).save(source)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    output, message = processing_window._process_one_photo(source, out_dir, builtin_profile("builtin:photo-iphone-15-pro"), "medium")
    assert output.exists() and output.stat().st_size > 0, message


def test_window_video_pipeline_without_audio(tools, tmp_path, processing_window):
    """A preset on a silent clip: audio fields are skipped, the file must still be produced."""
    source = tmp_path / "silent clip ü.mp4"
    subprocess.run([tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    output, message = processing_window._process_one_video(source, out_dir, "9:16", builtin_profile("builtin:video-iphone-12-pro"))
    assert output.exists() and output.stat().st_size > 1000, message
    assert "mismatch" not in message, message  # audio fields are skipped, not reported as wrong


def test_h264_in_avi_is_trimmed_and_encoded(tools, tmp_path):
    """Regression: input-side seeking produced an empty file for H.264 in AVI (FFmpeg 8.1)."""
    from uniqlab.app import has_video_stream, probe_duration, run_ffmpeg
    from uniqlab.media_metadata import build_plan, ffmpeg_video_options, video_container

    source = tmp_path / "clip.avi"
    subprocess.run([tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=30:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
    run_ffmpeg(source, tmp_path / "plain.mp4", "1:1")
    assert has_video_stream(tmp_path / "plain.mp4") and 2.4 < probe_duration(tmp_path / "plain.mp4") < 3.0
    profile = builtin_profile("builtin:video-iphone-12-pro")
    plan = build_plan(profile, "video", video_container(profile), source)
    output = tmp_path / "preset.mov"
    run_ffmpeg(source, output, "9:16", encode_options=ffmpeg_video_options(profile, plan))
    assert has_video_stream(output) and probe_duration(output) > 2.4


def test_output_without_frames_is_an_error(tools, tmp_path):
    from uniqlab.app import has_video_stream

    empty = tmp_path / "empty.mov"
    empty.write_bytes(b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  ")
    assert not has_video_stream(empty)
