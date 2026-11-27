"""Photo uniquification with Pillow: subtle pixel-level changes, same size and look.

Each output gets a slightly different crop that is resampled back to the exact original
dimensions, a small tone shift and fine zero-mean noise, so the pixel data and file hash
change while the image looks the same. Orientation (EXIF), the ICC colour profile and
transparency are preserved; metadata is handled afterwards by media_metadata.
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, JpegImagePlugin

from uniqlab.i18n import tr

PHOTO_EXTENSIONS = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}
OUTPUT_EXTENSIONS = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}

INTENSITY = {
    # crop share per side (min, max), tone shift (min, max), noise sigma
    "light": ((0.002, 0.005), (0.008, 0.015), 1.6),
    "medium": ((0.004, 0.009), (0.015, 0.025), 2.4),
    "strong": ((0.007, 0.0125), (0.025, 0.04), 3.4),
}
INTENSITY_KEYS = ("light", "medium", "strong")  # labels: i18n "photo.intensity.<key>"


class PhotoError(Exception):
    pass


def photo_format(path: Path) -> str | None:
    return PHOTO_EXTENSIONS.get(path.suffix.lower())


def _signed(rng: random.Random, low: float, high: float) -> float:
    return rng.choice((-1, 1)) * rng.uniform(low, high)


def _is_lossless_webp(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(64)
    return b"VP8L" in header


def process_photo(source: Path, output: Path, intensity: str = "medium", rng: random.Random | None = None) -> dict:
    rng = rng or random.Random()
    crop_range, tone_range, sigma = INTENSITY.get(intensity, INTENSITY["medium"])
    fmt = photo_format(source)
    if fmt is None:
        raise PhotoError(tr("photo.error.format"))
    try:
        original = Image.open(source)
        original.load()
    except Exception as exc:
        raise PhotoError(tr("photo.error.corrupt", error=exc)) from exc

    if getattr(original, "n_frames", 1) > 1:
        raise PhotoError(tr("photo.error.animated"))
    icc = original.info.get("icc_profile")
    detected = (original.format or "").lower()
    if detected not in OUTPUT_EXTENSIONS:
        raise PhotoError(tr("photo.error.content", format=original.format or "?"))

    image = original
    if image.mode == "P":
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    elif image.mode == "LA":
        image = image.convert("RGBA")
    elif image.mode == "CMYK":
        raise PhotoError(tr("photo.error.cmyk"))
    elif image.mode not in ("RGB", "RGBA", "L"):
        raise PhotoError(tr("photo.error.mode", mode=image.mode))

    width, height = image.size
    alpha = image.getchannel("A") if image.mode == "RGBA" else None
    base = image.convert("RGB") if image.mode == "RGBA" else image

    # 1) a slightly different framing, resampled back to the exact original size
    left = width * rng.uniform(*crop_range)
    right = width * (1 - rng.uniform(*crop_range))
    top = height * rng.uniform(*crop_range)
    bottom = height * (1 - rng.uniform(*crop_range))
    box = (left, top, right, bottom)
    base = base.resize((width, height), Image.LANCZOS, box=box)
    if alpha is not None:
        # Bilinear: no ringing, so transparency never overshoots at hard edges.
        alpha = alpha.resize((width, height), Image.BILINEAR, box=box)

    # 2) tiny tone shift
    base = ImageEnhance.Brightness(base).enhance(1 + _signed(rng, *tone_range))
    base = ImageEnhance.Contrast(base).enhance(1 + _signed(rng, *tone_range))
    if base.mode == "RGB":
        base = ImageEnhance.Color(base).enhance(1 + _signed(rng, *tone_range))

    # 3) fine zero-mean noise (Gaussian around 128, shifted back to 0)
    noise = Image.effect_noise((width, height), sigma)
    if base.mode == "RGB":
        noise = Image.merge("RGB", (noise, noise, noise))
    base = ImageChops.add(base, noise, scale=1.0, offset=-128)

    result = base
    if alpha is not None:
        result = base.convert("RGBA")
        result.putalpha(alpha)

    save = {}
    if icc:
        save["icc_profile"] = icc
    orientation = original.getexif().get(0x0112)
    if orientation and orientation != 1:
        # Keep how the picture is displayed even before any metadata step runs.
        exif = Image.Exif()
        exif[0x0112] = orientation
        save["exif"] = exif
    if detected == "jpeg":
        if result.mode == "RGBA":
            result = result.convert("RGB")
        sampling = JpegImagePlugin.get_sampling(original)
        save.update(quality=95, optimize=True, subsampling=sampling if sampling in (0, 1, 2) else 2,
                    progressive=bool(original.info.get("progressive") or original.info.get("progression")))
        result.save(output, "JPEG", **save)
    elif detected == "png":
        result.save(output, "PNG", **save)
    else:
        lossless = _is_lossless_webp(source)
        save.update(lossless=lossless, quality=100 if lossless else 92, method=6, exact=alpha is not None)
        result.save(output, "WEBP", **save)
    return {"format": detected, "width": width, "height": height, "alpha": alpha is not None, "icc": bool(icc)}
