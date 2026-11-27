"""Photo processing with Pillow: outputs are valid, subtly different and metadata-free."""

from __future__ import annotations

import random

import pytest
from PIL import Image, ImageChops

from uniqlab.photo_processing import INTENSITY_KEYS, PhotoError, process_photo


def _sample(path, fmt, mode="RGB"):
    image = Image.new(mode, (320, 240))
    for x in range(320):
        for y in range(0, 240, 8):
            image.putpixel((x, y), (x % 256, y % 256, (x + y) % 256) if mode == "RGB" else (x % 256, y % 256, 0, 255))
    image.save(path, fmt)
    return path


@pytest.mark.parametrize(("suffix", "fmt"), [(".jpg", "JPEG"), (".png", "PNG"), (".webp", "WEBP")])
@pytest.mark.parametrize("intensity", INTENSITY_KEYS)
def test_process_photo_formats(tmp_path, suffix, fmt, intensity):
    source = _sample(tmp_path / f"in{suffix}", fmt)
    output = tmp_path / f"out{suffix}"
    info = process_photo(source, output, intensity, rng=random.Random(7))
    with Image.open(output) as result:
        result.load()
        assert result.format == fmt
        assert (info["width"], info["height"]) == result.size
        assert result.size[0] <= 320 and result.size[1] <= 240
        assert "exif" not in result.info
    with Image.open(source) as original:
        resized = original.convert("RGB").resize(result.size)
    with Image.open(output) as result:
        assert ImageChops.difference(resized, result.convert("RGB")).getbbox() is not None


def test_rejects_unsupported_format(tmp_path):
    source = tmp_path / "in.gif"
    Image.new("RGB", (10, 10)).save(source, "GIF")
    with pytest.raises(PhotoError):
        process_photo(source, tmp_path / "out.gif")


def test_rejects_cmyk(tmp_path):
    source = tmp_path / "in.jpg"
    Image.new("CMYK", (40, 40)).save(source, "JPEG")
    with pytest.raises(PhotoError):
        process_photo(source, tmp_path / "out.jpg")


def test_rejects_corrupt_file(tmp_path):
    source = tmp_path / "in.png"
    source.write_bytes(b"not an image")
    with pytest.raises(PhotoError):
        process_photo(source, tmp_path / "out.png")
