"""Write metadata profiles to processed photos/videos and verify what actually got stored.

Container-level structures (brand, track handlers, timescales, timecode, Encoder tag,
codec signatures) are produced by FFmpeg while encoding; EXIF / XMP / QuickTime Keys are
written afterwards with ExifTool. Verification always reads the finished file back with
ExifTool — a successful write command is never taken as proof.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from uniqlab.dependencies import exiftool_command
from uniqlab.i18n import tr
from uniqlab.metadata_presets import field_spec, validate_value
from uniqlab.platform_support import run as run_process
_EXIFTOOL_PATH: str | None = None


def set_exiftool_path(path: str | None) -> None:
    """Chosen by the dependency manager (system, manual or UniqLab-managed install)."""
    global _EXIFTOOL_PATH
    _EXIFTOOL_PATH = path


def exiftool_available() -> bool:
    return bool(_EXIFTOOL_PATH) and Path(_EXIFTOOL_PATH).exists()


def run_exiftool(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    # Arguments go through a UTF-8 argfile so paths and values with any characters are safe.
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as handle:
        handle.write("\n".join(arg.replace("\n", " ") for arg in args) + "\n")
        argfile = handle.name
    try:
        if not exiftool_available():
            raise RuntimeError(tr("deps.exiftool_missing"))
        return run_process(
            exiftool_command(_EXIFTOOL_PATH) + ["-charset", "filename=utf8", "-@", argfile],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        Path(argfile).unlink(missing_ok=True)


class _Duplicates(list):
    """Values of a tag that ExifTool reported more than once (e.g. one per track)."""


def _collect_pairs(pairs):
    data: dict = {}
    for key, value in pairs:
        if key not in data:
            data[key] = value
        elif isinstance(data[key], _Duplicates):
            data[key].append(value)
        else:
            data[key] = _Duplicates([data[key], value])
    return data


def read_metadata(path: Path, numeric: bool = False) -> dict:
    """All tags of a file as {"Group:Tag": value}; duplicate tags become lists."""
    # -G1:4 adds a "CopyN" group to repeated tags (JSON would otherwise keep only one of them).
    args = ["-j", "-G1:4", "-a", "-s", "-api", "LargeFileSupport=1"]
    if numeric:
        args.append("-n")
    result = run_exiftool(args + [str(path)])
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or tr("meta.read_failed"))
    items = json.loads(result.stdout, object_pairs_hook=lambda pairs: _collect_pairs(
        [(re.sub(r":Copy\d+(?=:)", "", key), value) for key, value in pairs]))
    return items[0] if items else {}


# ------------------------------------------------------------------------- time


@dataclass
class Moment:
    local: datetime  # naive wall-clock time in the zone below
    offset: timedelta

    @property
    def offset_text(self) -> str:
        total = int(self.offset.total_seconds() // 60)
        sign = "+" if total >= 0 else "-"
        return f"{sign}{abs(total) // 60:02d}:{abs(total) % 60:02d}"

    @property
    def utc(self) -> datetime:
        return (self.local - self.offset).replace(tzinfo=timezone.utc)


def parse_offset(text: str) -> timedelta | None:
    match = re.fullmatch(r"\s*([+-])(\d\d):(\d\d)\s*", text or "")
    if not match:
        return None
    sign = 1 if match.group(1) == "+" else -1
    return sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))


def _parse_exif_datetime(text: str) -> tuple[datetime, timedelta | None] | None:
    match = re.match(r"(\d{4}):(\d\d):(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?", str(text or ""))
    if not match or match.group(1) == "0000":
        return None
    dt = datetime(*(int(match.group(index)) for index in range(1, 7)))
    tz = match.group(7)
    offset = timedelta(0) if tz == "Z" else parse_offset(tz) if tz else None
    return dt, offset


def resolve_moment(settings: dict, source: Path, media: str, source_meta: dict | None = None) -> tuple[Moment, str]:
    """Pick the timestamp for an output file according to the profile's time settings."""
    mode = (settings or {}).get("mode", "now")
    forced = parse_offset((settings or {}).get("offset", ""))
    now = datetime.now().astimezone()
    note = ""
    if mode == "custom":
        parsed = _parse_exif_datetime(settings.get("custom", ""))
        if parsed:
            local, offset = parsed
            return Moment(local, forced or offset or now.utcoffset()), tr("meta.time.custom")
        note = tr("meta.time.custom_invalid")
    elif mode == "source":
        meta = source_meta if source_meta is not None else _safe_read(source)
        candidates = (
            [("ExifIFD:DateTimeOriginal", "ExifIFD:OffsetTimeOriginal"), ("ExifIFD:CreateDate", "ExifIFD:OffsetTimeDigitized"),
             ("XMP-photoshop:DateCreated", None), ("IFD0:ModifyDate", "ExifIFD:OffsetTime")]
            if media == "photo"
            else [("Keys:CreationDate", None), ("QuickTime:CreateDate", "UTC")]
        )
        for date_key, offset_key in candidates:
            parsed = _parse_exif_datetime(meta.get(date_key, ""))
            if not parsed:
                continue
            local, offset = parsed
            if offset_key == "UTC":  # QuickTime header times are UTC
                instant = local.replace(tzinfo=timezone.utc).astimezone(timezone(forced) if forced else None)
                return Moment(instant.replace(tzinfo=None), instant.utcoffset()), tr("meta.time.source_utc")
            if offset is None and offset_key:
                offset = parse_offset(str(meta.get(offset_key, "")))
            return Moment(local, forced or offset or now.utcoffset()), tr("meta.time.source_tag", tag=date_key)
        note = tr("meta.time.source_missing")
    if forced is not None:
        instant = now.astimezone(timezone(forced))
        return Moment(instant.replace(tzinfo=None), forced), note or tr("meta.time.now")
    return Moment(now.replace(tzinfo=None), now.utcoffset()), note or tr("meta.time.now")


def _safe_read(path: Path) -> dict:
    try:
        return read_metadata(path)
    except Exception:
        return {}


# ------------------------------------------------------------------------- profile → plan


@dataclass
class Plan:
    media: str
    fmt: str
    fields: dict  # resolved ExifTool fields (Group:Tag -> value)
    ffmpeg: dict  # resolved FFMPEG:* options
    skipped: list = field(default_factory=list)  # (key, reason)
    moment: Moment | None = None
    moment_note: str = ""


def resolve_tokens(value: str, moment: Moment, ids: dict, fps: float | None) -> str:
    text = str(value)
    if "$" not in text:
        return text
    local = moment.local.strftime("%Y:%m:%d %H:%M:%S")
    replacements = {
        "$LOCAL_TZ": f"{local}{moment.offset_text}",
        "$LOCAL": local,
        "$OFFSET": moment.offset_text,
        "$SUBSEC": f"{moment.local.microsecond // 1000:03d}",
        "$XMP_DID": ids.setdefault("did", f"xmp.did:{uuid.uuid4()}"),
        "$XMP_IID": ids.setdefault("iid", f"xmp.iid:{uuid.uuid4()}"),
        "$FPS": f"{fps:.6f}" if fps else "",
    }
    for token, replacement in replacements.items():
        text = text.replace(token, replacement)
    return text


def build_plan(
    profile: dict,
    media: str,
    fmt: str,
    source: Path,
    fps: float | None = None,
    source_meta: dict | None = None,
    moment: tuple | None = None,
) -> Plan:
    # Pass the (moment, note) of an earlier plan so FFmpeg and ExifTool use the same time.
    moment, note = moment or resolve_moment(profile.get("timestamps", {}), source, media, source_meta)
    ids: dict = {}
    fields: dict = {}
    ffmpeg: dict = {}
    skipped: list = []
    for key, raw in (profile.get("fields") or {}).items():
        spec = field_spec(media, key)
        if spec is None:
            skipped.append((key, tr("meta.skip.unsupported")))
            continue
        effective_formats = spec.formats
        if key.startswith("FFMPEG:") and key != "FFMPEG:compressor_name":
            effective_formats = ("mov", "mp4")
        if fmt not in effective_formats:
            skipped.append((key, tr("meta.skip.format", format=fmt.upper())))
            continue
        error = validate_value(spec, raw)
        if error:
            skipped.append((key, tr("meta.skip.invalid", error=error)))
            continue
        value = resolve_tokens(raw, moment, ids, fps)
        if key == "FFMPEG:encoder" and value:
            # FFmpeg's bitexact mode drops ©too even when set, so ExifTool writes it.
            fields["UserData:Encoder" if fmt == "mov" else "ItemList:Encoder"] = value
        if value == "" and spec.kind != "bool" and key != "FFMPEG:timecode":
            skipped.append((key, tr("meta.skip.undefined")))
            continue
        (ffmpeg if key.startswith("FFMPEG:") else fields)[key] = value
    return Plan(media, fmt, fields, ffmpeg, skipped, moment, note)


def probe_fps(ffmpeg_path: str, path: Path) -> float | None:
    result = run_process([ffmpeg_path, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    match = re.search(r"Video:.*?(\d+(?:\.\d+)?) fps", result.stderr or "")
    return float(match.group(1)) if match else None


def video_container(profile: dict) -> str:
    return (profile.get("fields") or {}).get("FFMPEG:container", "mp4") if profile.get("kind") != "none" else "mp4"


def ffmpeg_video_options(profile: dict, plan: Plan | None) -> dict:
    """Encoder/muxer options for run_ffmpeg. Empty for the neutral profile (old behaviour)."""
    if profile.get("kind") == "none" or plan is None:
        return {}
    options = plan.ffmpeg
    codec = options.get("FFMPEG:codec", "h264")
    container = options.get("FFMPEG:container", "mp4")
    video_args = (
        ["-c:v", "libx265", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
         "-x265-params", "log-level=error" + (":info=0" if options.get("FFMPEG:strip_sei") == "1" else "")]
        if codec == "hevc"
        else ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    )
    mux = ["-fflags", "+bitexact"]  # no "Lavf…" encoder tag from FFmpeg itself
    if container == "mp4" and options.get("FFMPEG:brand", "").strip():
        mux += ["-brand", options["FFMPEG:brand"].strip()]
    if options.get("FFMPEG:handler_video"):
        mux += ["-metadata:s:v:0", f"handler_name={options['FFMPEG:handler_video']}"]
    if options.get("FFMPEG:handler_audio"):
        mux += ["-metadata:s:a:0", f"handler_name={options['FFMPEG:handler_audio']}"]
    # The stream "encoder" tag becomes the MOV compressor name; without it FFmpeg writes "Lavc libx264".
    mux += ["-metadata:s:v:0", f"encoder={options.get('FFMPEG:compressor_name', '')}"]
    if options.get("FFMPEG:movie_timescale"):
        mux += ["-movie_timescale", options["FFMPEG:movie_timescale"]]
    if options.get("FFMPEG:video_timescale"):
        mux += ["-video_track_timescale", options["FFMPEG:video_timescale"]]
    if options.get("FFMPEG:timecode"):
        mux += ["-timecode", options["FFMPEG:timecode"]]
    if plan.moment is not None:
        mux += ["-metadata", "creation_time=" + plan.moment.utc.strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
    bsf = []
    if options.get("FFMPEG:strip_sei") == "1":
        # Drops the x264/x265 "encoder settings" SEI that identifies the encoder.
        # (x265's version SEI lives in the hvcC header, so for HEVC it is disabled in the encoder above.)
        bsf = [] if codec == "hevc" else ["-bsf:v", "filter_units=remove_types=6"]
    audio_rate = options.get("FFMPEG:audio_rate", "keep")
    audio = ["-ar", audio_rate] if audio_rate in ("44100", "48000") else []
    return {"container": container, "video_args": video_args, "mux_args": mux + bsf, "audio_args": audio}


# ------------------------------------------------------------------------- writing


def _icc_is_wide(meta: dict) -> bool | None:
    description = str(meta.get("ICC_Profile:ProfileDescription", "")).lower()
    if not description:
        return None
    return not ("srgb" in description and "p3" not in description and "2020" not in description)


def exiftool_write_args(plan: Plan) -> list[str]:
    args = []
    for key, value in plan.fields.items():
        if key == "XMP-xmpMM:HistorySoftwareAgent":
            when = plan.moment.local.strftime("%Y:%m:%d %H:%M:%S") + plan.moment.offset_text if plan.moment else ""
            args.append(f"-XMP-xmpMM:History={{action=saved,softwareAgent={value},when={when},changed=/}}")
            continue
        args.append(f"-{key}={value}")
    return args


def write_photo_metadata(profile: dict, plan: Plan, source: Path, output: Path, source_meta: dict | None = None) -> list[str]:
    """Apply the profile to a processed photo. Returns log lines."""
    notes = []
    source_meta = source_meta if source_meta is not None else _safe_read(source)
    args = ["-overwrite_original", "-n", "-m"]
    keep = bool(profile.get("keep_source_metadata"))
    if keep:
        # Bring the source metadata over, minus parts that describe data the new file no longer has.
        args += ["-TagsFromFile", str(source), "-all:all", "--IFD1:all", "--ThumbnailImage", "--PreviewImage",
                 "--MPF:all", "--XMP-GContainer:all", "--XMP-hdrgm:all", "--XMP-GCamera:all", "--ExifIFD:ExifImageWidth",
                 "--ExifIFD:ExifImageHeight", "--ICC_Profile:all"]
    elif source_meta.get("IFD0:Orientation"):
        # Orientation is part of how the picture is displayed — always carried over.
        args += ["-TagsFromFile", str(source), "-IFD0:Orientation"]
    if plan.fields:
        from PIL import Image

        written = read_metadata(output, numeric=True)
        # ColorSpace must describe the ICC profile that is really embedded, not the preset's camera.
        args += ["-ExifIFD:ColorSpace=" + ("65535" if _icc_is_wide(written) else "1")]
        with Image.open(output) as image:
            width, height = image.size
        args += [f"-ExifIFD:ExifImageWidth={width}", f"-ExifIFD:ExifImageHeight={height}"]
        args += exiftool_write_args(plan)
    if len(args) <= 3:
        return notes
    result = run_exiftool(args + [str(output)])
    if result.returncode != 0:
        raise RuntimeError("ExifTool: " + (result.stderr.strip() or result.stdout.strip()))
    if result.stderr.strip():
        notes.append("ExifTool: " + " ".join(result.stderr.split())[:200])
    return notes


def write_video_metadata(profile: dict, plan: Plan, source: Path, output: Path) -> list[str]:
    notes = []
    args = ["-overwrite_original", "-n", "-m", "-api", "LargeFileSupport=1"]
    if plan.fields:
        args += exiftool_write_args(plan)
    if len(args) <= 5:
        return notes
    result = run_exiftool(args + [str(output)], timeout=600)
    if result.returncode != 0:
        raise RuntimeError("ExifTool: " + (result.stderr.strip() or result.stdout.strip()))
    if result.stderr.strip():
        notes.append("ExifTool: " + " ".join(result.stderr.split())[:200])
    return notes


# ------------------------------------------------------------------------- verification


@dataclass
class VerifyReport:
    checked: int = 0
    passed: list = field(default_factory=list)
    failed: list = field(default_factory=list)  # (key, expected, actual)
    skipped: list = field(default_factory=list)  # (key, reason)
    readable: bool = True
    readable_note: str = ""

    @property
    def ok(self) -> bool:
        return self.readable and not self.failed

    def summary(self) -> str:
        text = tr("meta.verify.summary", passed=len(self.passed), checked=self.checked)
        if self.skipped:
            text += tr("meta.verify.skipped", n=len(self.skipped))
        if self.failed:
            text += tr("meta.verify.mismatch") + "; ".join(
                tr("meta.verify.mismatch_item", key=key, expected=exp, actual=act) for key, exp, act in self.failed[:4]
            )
        if not self.readable:
            text += tr("meta.verify.unreadable") + self.readable_note
        return text


def _values(meta: dict, key: str) -> list:
    """All values for a tag; for "Track*:Tag" style checks, gather every track."""
    group, _, tag = key.partition(":")
    found = []
    for name, value in meta.items():
        name_group, _, name_tag = name.partition(":")
        if name_tag == tag and (name_group == group or group == "*" or (group == "Track*" and name_group.startswith("Track"))):
            found.extend(value if isinstance(value, list) else [value])
    return found


def _number(text: str) -> float:
    if re.fullmatch(r"-?\d+(\.\d+)?/\d+(\.\d+)?", text):
        top, bottom = text.split("/")
        return float(top) / float(bottom)
    return float(text)


def _matches(expected: str, actual) -> bool:
    if isinstance(actual, list):
        return any(_matches(expected, item) for item in actual)
    exp = str(expected).strip()
    act = str(actual).strip()
    if exp == act:
        return True
    try:
        a, b = _number(exp), _number(act)
        return math.isclose(a, b, rel_tol=0.01, abs_tol=0.011)
    except (ValueError, ZeroDivisionError):
        pass
    numbers_exp, numbers_act = exp.split(), act.split()
    if len(numbers_exp) > 1 and len(numbers_exp) == len(numbers_act):
        try:
            return all(math.isclose(float(x), float(y), rel_tol=0.01, abs_tol=0.011) for x, y in zip(numbers_exp, numbers_act))
        except ValueError:
            return False
    # ExifTool may add seconds fractions or normalise date separators.
    return re.sub(r"[-T]", ":", exp) == re.sub(r"[-T]", ":", act).split(".")[0]


FFMPEG_CHECKS = {
    "FFMPEG:handler_video": "Track*:HandlerDescription",
    "FFMPEG:handler_audio": "Track*:HandlerDescription",
    "FFMPEG:compressor_name": "Track*:CompressorName",
    "FFMPEG:encoder": "*:Encoder",
    "FFMPEG:movie_timescale": "QuickTime:TimeScale",
    "FFMPEG:video_timescale": "Track*:MediaTimeScale",
    "FFMPEG:audio_rate": "Track*:AudioSampleRate",
}
AUDIO_KEYS = ("FFMPEG:handler_audio", "FFMPEG:audio_rate")


def _contains_encoder_sei(path: Path) -> bool:
    with path.open("rb") as handle:
        chunk = handle.read(8 * 1024 * 1024)
    return b"x264 - core" in chunk or b"x265 (build" in chunk


def verify_output(plan: Plan, output: Path, ffmpeg_path: str) -> VerifyReport:
    report = VerifyReport(skipped=list(plan.skipped))
    # 1) still a valid, readable media file
    if plan.media == "video":
        check = run_process([ffmpeg_path, "-v", "error", "-i", str(output), "-map", "0", "-c", "copy", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=600)
        if check.returncode != 0:
            report.readable, report.readable_note = False, check.stderr.strip()[:200]
    else:
        try:
            from PIL import Image

            with Image.open(output) as image:
                image.load()
        except Exception as exc:
            report.readable, report.readable_note = False, str(exc)[:200]
    has_audio = True
    if plan.media == "video" and any(key in plan.ffmpeg for key in AUDIO_KEYS):
        probe = run_process([ffmpeg_path, "-hide_banner", "-i", str(output)], capture_output=True, text=True, timeout=120)
        has_audio = "Audio:" in (probe.stderr or "")
    meta = read_metadata(output, numeric=True)
    # 2) every requested field, read back from the file
    for key, expected in plan.fields.items():
        report.checked += 1
        lookup = "XMP-xmpMM:HistorySoftwareAgent" if key == "XMP-xmpMM:HistorySoftwareAgent" else key
        values = _values(meta, lookup)
        if values and _matches(expected, values):
            report.passed.append(key)
        else:
            report.failed.append((key, expected, values[0] if values else tr("meta.verify.absent")))
    for key, expected in plan.ffmpeg.items():
        if key in ("FFMPEG:codec", "FFMPEG:container", "FFMPEG:brand", "FFMPEG:timecode", "FFMPEG:strip_sei"):
            report.checked += 1
            ok, actual = _verify_special(key, expected, meta, output)
        elif key in FFMPEG_CHECKS:
            if key == "FFMPEG:audio_rate" and expected == "keep" or key == "FFMPEG:encoder" and not expected:
                continue
            if key == "FFMPEG:compressor_name" and not expected:
                continue
            if key in AUDIO_KEYS and not has_audio:
                # A silent clip has no audio track to carry these values.
                report.skipped.append((key, tr("meta.skip.no_audio")))
                continue
            report.checked += 1
            values = _values(meta, FFMPEG_CHECKS[key])
            ok, actual = (bool(values) and _matches(expected, values)), (values[0] if values else tr("meta.verify.absent"))
        else:
            continue
        if ok:
            report.passed.append(key)
        else:
            report.failed.append((key, expected, actual))
    return report


def _verify_special(key: str, expected: str, meta: dict, output: Path) -> tuple[bool, str]:
    if key == "FFMPEG:codec":
        ids = _values(meta, "Track*:CompressorID")
        want = "hvc1" if expected == "hevc" else "avc1"
        return want in ids, ", ".join(map(str, ids)) or tr("meta.verify.absent")
    if key == "FFMPEG:container":
        actual = str(meta.get("File:FileType", "")).lower()
        return actual == expected, actual
    if key == "FFMPEG:brand":
        actual = str(meta.get("QuickTime:MajorBrand", ""))
        return actual.strip() == expected.strip(), actual
    if key == "FFMPEG:timecode":
        has_tmcd = "tmcd" in [str(v) for v in _values(meta, "Track*:HandlerType")] or "tmcd" in [str(v) for v in _values(meta, "Track*:OtherFormat")]
        return (has_tmcd if expected else not has_tmcd), (tr("meta.verify.tmcd_yes") if has_tmcd else tr("meta.verify.tmcd_no"))
    if key == "FFMPEG:strip_sei":
        present = _contains_encoder_sei(output)
        return (not present) if expected == "1" else True, (tr("meta.verify.sei_yes") if present else tr("meta.verify.sei_no"))
    return False, "?"
