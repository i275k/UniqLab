"""Supported metadata fields and the built-in, evidence-based metadata presets.

Every built-in preset lists where its values come from (real sample files inspected with
ExifTool 13.63 in November 2026) and what cannot be reproduced. Values that vary per shot
(exposure, ISO, GPS, ...) or that are proprietary (MakerNotes, Samsung trailers, Apple
private tracks) are deliberately not part of any preset.

All user-facing texts (field labels, groups, notes, option labels, preset names,
summaries, sources and limitations) are i18n keys resolved through ``uniqlab.i18n``.

Field values may contain tokens that are resolved per output file:
  $LOCAL      local capture/creation time  "YYYY:MM:DD HH:MM:SS"
  $LOCAL_TZ   the same with offset          "YYYY:MM:DD HH:MM:SS+HH:MM"
  $OFFSET     time-zone offset              "+HH:MM"
  $SUBSEC     milliseconds of that time     "123"
  $XMP_DID    new "xmp.did:<uuid>"; $XMP_IID new "xmp.iid:<uuid>"
  $FPS        real frame rate of the output, "30.000000"
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from uniqlab.i18n import tr

PHOTO_FORMATS = ("jpeg", "png", "webp")
VIDEO_FORMATS = ("mov", "mp4")


@dataclass(frozen=True)
class FieldSpec:
    key: str  # "<group>:<tag>" as ExifTool writes/reads it, or "FFMPEG:<option>"
    group: str  # i18n slug under "fgroup."
    media: str  # "photo" | "video"
    kind: str  # text | int | number | enum | datetime | offset | subsec | version | lensinfo | bool
    formats: tuple = ()
    options: tuple = ()  # allowed values for enum
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 120
    has_note: bool = False

    @property
    def i18n(self) -> str:
        return f"field.{self.media}.{self.key.replace(':', '__')}"

    @property
    def label(self) -> str:
        return tr(f"{self.i18n}.label")

    @property
    def note(self) -> str:
        return tr(f"{self.i18n}.note") if self.has_note else ""

    @property
    def group_label(self) -> str:
        return tr(f"fgroup.{self.group}")

    def option_label(self, value: str) -> str:
        return tr(f"{self.i18n}.options.{str(value).strip() or 'blank'}")


FIELDS: list[FieldSpec] = [
    FieldSpec('IFD0:Make', 'device', 'photo', 'text', ('jpeg', 'png', 'webp')),
    FieldSpec('IFD0:Model', 'device', 'photo', 'text', ('jpeg', 'png', 'webp')),
    FieldSpec('IFD0:Software', 'device', 'photo', 'text', ('jpeg', 'png', 'webp')),
    FieldSpec('IFD0:HostComputer', 'device', 'photo', 'text', ('jpeg', 'png', 'webp'), has_note=True),
    FieldSpec('ExifIFD:ExifVersion', 'device', 'photo', 'version', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:FlashpixVersion', 'device', 'photo', 'version', ('jpeg', 'png', 'webp')),
    FieldSpec('IFD0:XResolution', 'device', 'photo', 'number', ('jpeg', 'png', 'webp'), minimum=1, maximum=10000),
    FieldSpec('IFD0:YResolution', 'device', 'photo', 'number', ('jpeg', 'png', 'webp'), minimum=1, maximum=10000),
    FieldSpec('IFD0:ResolutionUnit', 'device', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('2', '3')),
    FieldSpec('IFD0:YCbCrPositioning', 'device', 'photo', 'enum', ('jpeg',), options=('1', '2')),
    FieldSpec('ExifIFD:ComponentsConfiguration', 'device', 'photo', 'text', ('jpeg',), has_note=True),
    FieldSpec('ExifIFD:LensMake', 'lens', 'photo', 'text', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:LensModel', 'lens', 'photo', 'text', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:LensInfo', 'lens', 'photo', 'lensinfo', ('jpeg', 'png', 'webp'), has_note=True),
    FieldSpec('ExifIFD:FocalLength', 'lens', 'photo', 'number', ('jpeg', 'png', 'webp'), minimum=0.1, maximum=2000),
    FieldSpec('ExifIFD:FocalLengthIn35mmFormat', 'lens', 'photo', 'int', ('jpeg', 'png', 'webp'), minimum=1, maximum=5000),
    FieldSpec('ExifIFD:FNumber', 'lens', 'photo', 'number', ('jpeg', 'png', 'webp'), minimum=0.5, maximum=64),
    FieldSpec('ExifIFD:ApertureValue', 'lens', 'photo', 'number', ('jpeg', 'png', 'webp'), minimum=0.5, maximum=64),
    FieldSpec('ExifIFD:MaxApertureValue', 'lens', 'photo', 'number', ('jpeg', 'png', 'webp'), minimum=0.5, maximum=64),
    FieldSpec('ExifIFD:DigitalZoomRatio', 'capture', 'photo', 'number', ('jpeg', 'png', 'webp'), minimum=0, maximum=100),
    FieldSpec('ExifIFD:ExposureProgram', 'capture', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('1', '2', '3', '4')),
    FieldSpec('ExifIFD:MeteringMode', 'capture', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('1', '2', '3', '5')),
    FieldSpec('ExifIFD:Flash', 'capture', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('0', '16', '24', '25')),
    FieldSpec('ExifIFD:WhiteBalance', 'capture', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('0', '1')),
    FieldSpec('ExifIFD:ExposureMode', 'capture', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('0', '1', '2')),
    FieldSpec('ExifIFD:SceneCaptureType', 'capture', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('0', '1', '2', '3')),
    FieldSpec('ExifIFD:SensingMethod', 'capture', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('2',)),
    FieldSpec('ExifIFD:SceneType', 'capture', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('1',)),
    FieldSpec('ExifIFD:CompositeImage', 'capture', 'photo', 'enum', ('jpeg', 'png', 'webp'), options=('0', '1', '2', '3')),
    FieldSpec('ExifIFD:ExposureTime', 'per_shot', 'photo', 'number', ('jpeg', 'png', 'webp'), minimum=1e-05, maximum=3600, has_note=True),
    FieldSpec('ExifIFD:ISO', 'per_shot', 'photo', 'int', ('jpeg', 'png', 'webp'), minimum=1, maximum=409600, has_note=True),
    FieldSpec('IFD0:ModifyDate', 'time', 'photo', 'datetime', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:DateTimeOriginal', 'time', 'photo', 'datetime', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:CreateDate', 'time', 'photo', 'datetime', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:OffsetTime', 'time', 'photo', 'offset', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:OffsetTimeOriginal', 'time', 'photo', 'offset', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:OffsetTimeDigitized', 'time', 'photo', 'offset', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:SubSecTime', 'time', 'photo', 'subsec', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:SubSecTimeOriginal', 'time', 'photo', 'subsec', ('jpeg', 'png', 'webp')),
    FieldSpec('ExifIFD:SubSecTimeDigitized', 'time', 'photo', 'subsec', ('jpeg', 'png', 'webp')),
    FieldSpec('XMP-x:XMPToolkit', 'xmp', 'photo', 'text', ('jpeg', 'png', 'webp'), has_note=True),
    FieldSpec('XMP-xmp:CreatorTool', 'xmp', 'photo', 'text', ('jpeg', 'png', 'webp')),
    FieldSpec('XMP-xmp:CreateDate', 'xmp', 'photo', 'datetime', ('jpeg', 'png', 'webp')),
    FieldSpec('XMP-xmp:ModifyDate', 'xmp', 'photo', 'datetime', ('jpeg', 'png', 'webp')),
    FieldSpec('XMP-photoshop:DateCreated', 'xmp', 'photo', 'datetime', ('jpeg', 'png', 'webp')),
    FieldSpec('IFD0:Artist', 'description', 'photo', 'text', ('jpeg', 'png', 'webp')),
    FieldSpec('IFD0:Copyright', 'description', 'photo', 'text', ('jpeg', 'png', 'webp')),
    FieldSpec('IFD0:ImageDescription', 'description', 'photo', 'text', ('jpeg', 'png', 'webp'), max_length=500),
    FieldSpec('Keys:Make', 'device', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('Keys:Model', 'device', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('Keys:Software', 'device', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('Keys:CreationDate', 'time', 'video', 'datetime', ('mov', 'mp4')),
    FieldSpec('Keys:AndroidVersion', 'device', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('Keys:AndroidCaptureFPS', 'capture', 'video', 'number', ('mov', 'mp4'), minimum=1, maximum=960),
    FieldSpec('XMP-x:XMPToolkit', 'xmp', 'video', 'text', ('mov', 'mp4'), has_note=True),
    FieldSpec('XMP-xmp:CreatorTool', 'xmp', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('XMP-xmp:CreateDate', 'xmp', 'video', 'datetime', ('mov', 'mp4')),
    FieldSpec('XMP-xmp:ModifyDate', 'xmp', 'video', 'datetime', ('mov', 'mp4')),
    FieldSpec('XMP-xmp:MetadataDate', 'xmp', 'video', 'datetime', ('mov', 'mp4')),
    FieldSpec('XMP-xmpMM:DocumentID', 'xmp', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('XMP-xmpMM:InstanceID', 'xmp', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('XMP-xmpMM:OriginalDocumentID', 'xmp', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('XMP-xmpMM:HistorySoftwareAgent', 'xmp', 'video', 'text', ('mov', 'mp4'), has_note=True),
    FieldSpec('XMP-xmpDM:VideoFrameRate', 'xmp', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('XMP-xmpDM:VideoFieldOrder', 'xmp', 'video', 'enum', ('mov', 'mp4'), options=('Progressive', 'Upper', 'Lower')),
    FieldSpec('XMP-xmpDM:VideoPixelAspectRatio', 'xmp', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('XMP-xmpDM:StartTimecodeTimeValue', 'xmp', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('ItemList:Title', 'description', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('ItemList:Comment', 'description', 'video', 'text', ('mov', 'mp4'), max_length=500),
    FieldSpec('FFMPEG:container', 'container', 'video', 'enum', ('mov', 'mp4'), options=('mp4', 'mov')),
    FieldSpec('FFMPEG:brand', 'container', 'video', 'enum', ('mov', 'mp4'), options=('isom', 'mp42', 'qt  '), has_note=True),
    FieldSpec('FFMPEG:codec', 'container', 'video', 'enum', ('mov', 'mp4'), options=('h264', 'hevc')),
    FieldSpec('FFMPEG:audio_rate', 'container', 'video', 'enum', ('mov', 'mp4'), options=('keep', '44100', '48000')),
    FieldSpec('FFMPEG:handler_video', 'container', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('FFMPEG:handler_audio', 'container', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('FFMPEG:compressor_name', 'container', 'video', 'text', ('mov',), max_length=31),
    FieldSpec('FFMPEG:encoder', 'container', 'video', 'text', ('mov', 'mp4')),
    FieldSpec('FFMPEG:movie_timescale', 'container', 'video', 'int', ('mov', 'mp4'), minimum=1, maximum=1000000),
    FieldSpec('FFMPEG:video_timescale', 'container', 'video', 'int', ('mov', 'mp4'), minimum=1, maximum=1000000),
    FieldSpec('FFMPEG:timecode', 'container', 'video', 'text', ('mov', 'mp4'), has_note=True),
    FieldSpec('FFMPEG:strip_sei', 'container', 'video', 'bool', ('mov', 'mp4')),
]

FIELD_INDEX: dict[tuple[str, str], FieldSpec] = {(spec.media, spec.key): spec for spec in FIELDS}
TOKENS = ("$LOCAL_TZ", "$LOCAL", "$OFFSET", "$SUBSEC", "$XMP_DID", "$XMP_IID", "$FPS")


def field_spec(media: str, key: str) -> FieldSpec | None:
    return FIELD_INDEX.get((media, key))


def validate_value(spec: FieldSpec, value: str) -> str | None:
    """Return an error message, or None when the value is acceptable for this field."""
    value = str(value).strip()
    if any(token in value for token in TOKENS):
        return None
    if spec.kind == "bool":
        return None if value in ("0", "1") else tr("validate.bool")
    if not value:
        return None if spec.kind == "text" or spec.key == "FFMPEG:timecode" else tr("validate.empty")
    if len(value) > spec.max_length:
        return tr("validate.max_length", n=spec.max_length)
    if spec.kind == "enum":
        allowed = [option.strip() for option in spec.options]
        return None if value in allowed else tr("validate.allowed", values=", ".join(spec.option_label(v) for v in spec.options))
    if spec.kind in ("int", "number"):
        try:
            number = int(value) if spec.kind == "int" else float(value)
        except ValueError:
            return tr("validate.int") if spec.kind == "int" else tr("validate.number")
        if spec.minimum is not None and number < spec.minimum or spec.maximum is not None and number > spec.maximum:
            return tr("validate.range", minimum=f"{spec.minimum:g}", maximum=f"{spec.maximum:g}")
        return None
    if spec.kind == "datetime":
        return None if re.fullmatch(r"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d([+-]\d\d:\d\d)?", value) else tr("validate.datetime")
    if spec.kind == "offset":
        return None if re.fullmatch(r"[+-](0\d|1[0-4]):[0-5]\d", value) else tr("validate.offset")
    if spec.kind == "subsec":
        return None if re.fullmatch(r"\d{1,6}", value) else tr("validate.subsec")
    if spec.kind == "version":
        return None if re.fullmatch(r"\d{4}", value) else tr("validate.version")
    if spec.kind == "lensinfo":
        parts = value.split()
        try:
            ok = len(parts) == 4 and all(float(part) > 0 for part in parts)
        except ValueError:
            ok = False
        return None if ok else tr("validate.lensinfo")
    if spec.key == "FFMPEG:timecode":
        return None if re.fullmatch(r"\d\d:[0-5]\d:[0-5]\d[:;]\d\d", value) else tr("validate.timecode")
    return None


def _photo_times(subsec_fields=("ExifIFD:SubSecTimeOriginal", "ExifIFD:SubSecTimeDigitized"), digitized_offset=True):
    fields = {
        "IFD0:ModifyDate": "$LOCAL",
        "ExifIFD:DateTimeOriginal": "$LOCAL",
        "ExifIFD:CreateDate": "$LOCAL",
        "ExifIFD:OffsetTime": "$OFFSET",
        "ExifIFD:OffsetTimeOriginal": "$OFFSET",
    }
    if digitized_offset:
        fields["ExifIFD:OffsetTimeDigitized"] = "$OFFSET"
    for key in subsec_fields:
        fields[key] = "$SUBSEC"
    return fields


_COMMON_RESOLUTION = {"IFD0:XResolution": "72", "IFD0:YResolution": "72", "IFD0:ResolutionUnit": "2"}

def _profile(**data):
    data.setdefault("builtin", True)
    data.setdefault("available", True)
    data.setdefault("keep_source_metadata", False)
    data.setdefault("timestamps", {"mode": "now", "custom": "", "offset": ""})
    data.setdefault("limitations", [])
    data.setdefault("evidence", [])
    return data


BUILTIN_PROFILES: list[dict] = [
    _profile(
        id='builtin:video-none',
        media='video',
        kind='none',
        fields={},
        name_key='preset.video-none.name',
        summary_key='preset.video-none.summary',
        group='group.none',
        evidence=[{'key': 'preset.video-none.evidence.1'}],
    ),
    _profile(
        id='builtin:photo-none',
        media='photo',
        kind='none',
        fields={},
        name_key='preset.photo-none.name',
        summary_key='preset.photo-none.summary',
        group='group.none',
    ),
    _profile(
        id='builtin:photo-iphone-15-pro',
        media='photo',
        kind='camera',
        fields={'IFD0:Make': 'Apple',
         'IFD0:Model': 'iPhone 15 Pro',
         'IFD0:Software': '18.6.2',
         'IFD0:HostComputer': 'iPhone 15 Pro',
         'IFD0:XResolution': '72',
         'IFD0:YResolution': '72',
         'IFD0:ResolutionUnit': '2',
         'IFD0:YCbCrPositioning': '1',
         'ExifIFD:ExifVersion': '0232',
         'ExifIFD:FlashpixVersion': '0100',
         'ExifIFD:ComponentsConfiguration': '1 2 3 0',
         'ExifIFD:LensMake': 'Apple',
         'ExifIFD:LensModel': 'iPhone 15 Pro back triple camera 6.765mm f/1.78',
         'ExifIFD:LensInfo': '6.764999866 9 1.779999971 2.8',
         'ExifIFD:FocalLength': '6.765',
         'ExifIFD:FocalLengthIn35mmFormat': '24',
         'ExifIFD:FNumber': '1.78',
         'ExifIFD:ApertureValue': '1.78',
         'ExifIFD:ExposureProgram': '2',
         'ExifIFD:MeteringMode': '5',
         'ExifIFD:Flash': '16',
         'ExifIFD:WhiteBalance': '0',
         'ExifIFD:ExposureMode': '0',
         'ExifIFD:SceneCaptureType': '0',
         'ExifIFD:SensingMethod': '2',
         'ExifIFD:SceneType': '1',
         'ExifIFD:CompositeImage': '2',
         'IFD0:ModifyDate': '$LOCAL',
         'ExifIFD:DateTimeOriginal': '$LOCAL',
         'ExifIFD:CreateDate': '$LOCAL',
         'ExifIFD:OffsetTime': '$OFFSET',
         'ExifIFD:OffsetTimeOriginal': '$OFFSET',
         'ExifIFD:OffsetTimeDigitized': '$OFFSET',
         'ExifIFD:SubSecTimeOriginal': '$SUBSEC',
         'ExifIFD:SubSecTimeDigitized': '$SUBSEC',
         'XMP-x:XMPToolkit': 'XMP Core 6.0.0',
         'XMP-xmp:CreatorTool': '18.6.2',
         'XMP-xmp:CreateDate': '$LOCAL',
         'XMP-xmp:ModifyDate': '$LOCAL',
         'XMP-photoshop:DateCreated': '$LOCAL'},
        name_key='preset.photo-iphone-15-pro.name',
        summary_key='preset.photo-iphone-15-pro.summary',
        group='group.apple_iphone',
        evidence=[{'key': 'preset.photo-iphone-15-pro.evidence.1',
          'url': 'https://commons.wikimedia.org/wiki/File:%22monument%22_for_V._Tatlin,_CR_102.jpg'},
         {'key': 'preset.photo-iphone-15-pro.evidence.2',
          'url': 'https://commons.wikimedia.org/wiki/Category:Taken_with_iPhone_15_Pro'}],
        limitations=['limits.apple_photo.1', 'limits.apple_photo.2', 'limits.apple_photo.3', 'limits.apple_photo.4'],
    ),
    _profile(
        id='builtin:photo-iphone-16-pro',
        media='photo',
        kind='camera',
        fields={'IFD0:Make': 'Apple',
         'IFD0:Model': 'iPhone 16 Pro',
         'IFD0:Software': '18.1.1',
         'IFD0:HostComputer': 'iPhone 16 Pro',
         'IFD0:XResolution': '72',
         'IFD0:YResolution': '72',
         'IFD0:ResolutionUnit': '2',
         'IFD0:YCbCrPositioning': '1',
         'ExifIFD:ExifVersion': '0232',
         'ExifIFD:FlashpixVersion': '0100',
         'ExifIFD:ComponentsConfiguration': '1 2 3 0',
         'ExifIFD:LensMake': 'Apple',
         'ExifIFD:LensModel': 'iPhone 16 Pro back triple camera 6.765mm f/1.78',
         'ExifIFD:LensInfo': '2.220000029 15.65999985 1.779999971 2.8',
         'ExifIFD:FocalLength': '6.765',
         'ExifIFD:FocalLengthIn35mmFormat': '24',
         'ExifIFD:FNumber': '1.78',
         'ExifIFD:ApertureValue': '1.78',
         'ExifIFD:ExposureProgram': '2',
         'ExifIFD:MeteringMode': '5',
         'ExifIFD:Flash': '16',
         'ExifIFD:WhiteBalance': '0',
         'ExifIFD:ExposureMode': '0',
         'ExifIFD:SceneCaptureType': '0',
         'ExifIFD:SensingMethod': '2',
         'ExifIFD:SceneType': '1',
         'ExifIFD:CompositeImage': '2',
         'IFD0:ModifyDate': '$LOCAL',
         'ExifIFD:DateTimeOriginal': '$LOCAL',
         'ExifIFD:CreateDate': '$LOCAL',
         'ExifIFD:OffsetTime': '$OFFSET',
         'ExifIFD:OffsetTimeOriginal': '$OFFSET',
         'ExifIFD:OffsetTimeDigitized': '$OFFSET',
         'ExifIFD:SubSecTimeOriginal': '$SUBSEC',
         'ExifIFD:SubSecTimeDigitized': '$SUBSEC',
         'XMP-x:XMPToolkit': 'XMP Core 6.0.0',
         'XMP-xmp:CreatorTool': '18.1.1',
         'XMP-xmp:CreateDate': '$LOCAL',
         'XMP-xmp:ModifyDate': '$LOCAL',
         'XMP-photoshop:DateCreated': '$LOCAL'},
        name_key='preset.photo-iphone-16-pro.name',
        summary_key='preset.photo-iphone-16-pro.summary',
        group='group.apple_iphone',
        evidence=[{'key': 'preset.photo-iphone-16-pro.evidence.1',
          'url': 'https://commons.wikimedia.org/wiki/File:03-07-2025_Warszawa_S%C5%82omi%C5%84skiego,_remont_tor%C3%B3w_tramwajowych,_1.jpg'},
         {'key': 'preset.photo-iphone-16-pro.evidence.2',
          'url': 'https://commons.wikimedia.org/wiki/Category:Taken_with_iPhone_16_Pro'}],
        limitations=['limits.apple_photo.1', 'limits.apple_photo.2', 'limits.apple_photo.3', 'limits.apple_photo.4'],
    ),
    _profile(
        id='builtin:photo-galaxy-s25-ultra',
        media='photo',
        kind='camera',
        fields={'IFD0:Make': 'samsung',
         'IFD0:Model': 'Galaxy S25 Ultra',
         'IFD0:Software': 'S938BXXS7BYKS',
         'IFD0:XResolution': '72',
         'IFD0:YResolution': '72',
         'IFD0:ResolutionUnit': '2',
         'IFD0:YCbCrPositioning': '1',
         'ExifIFD:ExifVersion': '0220',
         'ExifIFD:FlashpixVersion': '0100',
         'ExifIFD:FocalLength': '6.3',
         'ExifIFD:FocalLengthIn35mmFormat': '23',
         'ExifIFD:FNumber': '1.7',
         'ExifIFD:ApertureValue': '1.7',
         'ExifIFD:MaxApertureValue': '1.7',
         'ExifIFD:DigitalZoomRatio': '1',
         'ExifIFD:ExposureProgram': '2',
         'ExifIFD:MeteringMode': '2',
         'ExifIFD:Flash': '0',
         'ExifIFD:WhiteBalance': '0',
         'ExifIFD:ExposureMode': '0',
         'ExifIFD:SceneCaptureType': '0',
         'IFD0:ModifyDate': '$LOCAL',
         'ExifIFD:DateTimeOriginal': '$LOCAL',
         'ExifIFD:CreateDate': '$LOCAL',
         'ExifIFD:OffsetTime': '$OFFSET',
         'ExifIFD:OffsetTimeOriginal': '$OFFSET',
         'ExifIFD:SubSecTime': '$SUBSEC',
         'ExifIFD:SubSecTimeOriginal': '$SUBSEC',
         'ExifIFD:SubSecTimeDigitized': '$SUBSEC'},
        name_key='preset.photo-galaxy-s25-ultra.name',
        summary_key='preset.photo-galaxy-s25-ultra.summary',
        group='group.samsung_galaxy',
        evidence=[{'key': 'preset.photo-galaxy-s25-ultra.evidence.1',
          'url': 'https://commons.wikimedia.org/wiki/File:2025_hangar_Heksagon_MLP_01.jpg'},
         {'key': 'preset.photo-galaxy-s25-ultra.evidence.2',
          'url': 'https://commons.wikimedia.org/wiki/Category:Taken_with_Samsung_Galaxy_S25_Ultra'}],
        limitations=['limits.samsung_photo.1',
         'limits.samsung_photo.2',
         'limits.samsung_photo.3',
         'limits.samsung_photo.4',
         'limits.samsung_photo.5'],
    ),
    _profile(
        id='builtin:photo-galaxy-s24',
        media='photo',
        kind='camera',
        fields={'IFD0:Make': 'samsung',
         'IFD0:Model': 'Galaxy S24',
         'IFD0:Software': 'S921BXXS9BYH2',
         'IFD0:XResolution': '72',
         'IFD0:YResolution': '72',
         'IFD0:ResolutionUnit': '2',
         'IFD0:YCbCrPositioning': '1',
         'ExifIFD:ExifVersion': '0220',
         'ExifIFD:FlashpixVersion': '0100',
         'ExifIFD:FocalLength': '5.4',
         'ExifIFD:FocalLengthIn35mmFormat': '23',
         'ExifIFD:FNumber': '1.8',
         'ExifIFD:ApertureValue': '1.8',
         'ExifIFD:MaxApertureValue': '1.8',
         'ExifIFD:DigitalZoomRatio': '1',
         'ExifIFD:ExposureProgram': '2',
         'ExifIFD:MeteringMode': '2',
         'ExifIFD:Flash': '0',
         'ExifIFD:WhiteBalance': '0',
         'ExifIFD:ExposureMode': '0',
         'ExifIFD:SceneCaptureType': '0',
         'IFD0:ModifyDate': '$LOCAL',
         'ExifIFD:DateTimeOriginal': '$LOCAL',
         'ExifIFD:CreateDate': '$LOCAL',
         'ExifIFD:OffsetTime': '$OFFSET',
         'ExifIFD:OffsetTimeOriginal': '$OFFSET',
         'ExifIFD:SubSecTime': '$SUBSEC',
         'ExifIFD:SubSecTimeOriginal': '$SUBSEC',
         'ExifIFD:SubSecTimeDigitized': '$SUBSEC'},
        name_key='preset.photo-galaxy-s24.name',
        summary_key='preset.photo-galaxy-s24.summary',
        group='group.samsung_galaxy',
        evidence=[{'key': 'preset.photo-galaxy-s24.evidence.1',
          'url': 'https://commons.wikimedia.org/wiki/File:1bar_Kauppurienkatu_5_Oulu_20250922.jpg'},
         {'key': 'preset.photo-galaxy-s24.evidence.2',
          'url': 'https://commons.wikimedia.org/wiki/Category:Taken_with_Samsung_Galaxy_S24'},
         {'key': 'preset.photo-galaxy-s24.evidence.3',
          'url': 'https://github.com/immich-app/test-assets/tree/main/formats/motionphoto'}],
        limitations=['limits.samsung_photo.1',
         'limits.samsung_photo.2',
         'limits.samsung_photo.3',
         'limits.samsung_photo.4',
         'limits.samsung_photo.5'],
    ),
    _profile(
        id='builtin:video-iphone-12-pro',
        media='video',
        kind='camera',
        fields={'FFMPEG:container': 'mov',
         'FFMPEG:brand': 'qt  ',
         'FFMPEG:codec': 'hevc',
         'FFMPEG:audio_rate': '44100',
         'FFMPEG:handler_video': 'Core Media Video',
         'FFMPEG:handler_audio': 'Core Media Audio',
         'FFMPEG:compressor_name': 'HEVC',
         'FFMPEG:movie_timescale': '600',
         'FFMPEG:video_timescale': '600',
         'FFMPEG:strip_sei': '1',
         'Keys:Make': 'Apple',
         'Keys:Model': 'iPhone 12 Pro',
         'Keys:Software': '17.3.1',
         'Keys:CreationDate': '$LOCAL_TZ'},
        name_key='preset.video-iphone-12-pro.name',
        summary_key='preset.video-iphone-12-pro.summary',
        group='group.apple_iphone',
        evidence=[{'key': 'preset.video-iphone-12-pro.evidence.1',
          'url': 'https://github.com/immich-app/test-assets/blob/main/videos/train.mov'}],
        limitations=['preset.video-iphone-12-pro.limit.1',
         'preset.video-iphone-12-pro.limit.2',
         'preset.video-iphone-12-pro.limit.3',
         'preset.video-iphone-12-pro.limit.4'],
    ),
    _profile(
        id='builtin:video-galaxy-android14',
        media='video',
        kind='camera',
        fields={'FFMPEG:container': 'mp4',
         'FFMPEG:brand': 'mp42',
         'FFMPEG:codec': 'hevc',
         'FFMPEG:audio_rate': '48000',
         'FFMPEG:handler_video': 'VideoHandle',
         'FFMPEG:handler_audio': 'SoundHandle',
         'FFMPEG:movie_timescale': '10000',
         'FFMPEG:video_timescale': '90000',
         'FFMPEG:strip_sei': '1',
         'Keys:AndroidVersion': '14'},
        name_key='preset.video-galaxy-android14.name',
        summary_key='preset.video-galaxy-android14.summary',
        group='group.samsung_galaxy',
        evidence=[{'key': 'preset.video-galaxy-android14.evidence.1',
          'url': 'https://github.com/immich-app/test-assets/blob/main/formats/motionphoto/samsung-one-ui-6.jpg'},
         {'key': 'preset.video-galaxy-android14.evidence.2',
          'url': 'https://exiftool.org/forum/index.php?msg=74161'},
         {'key': 'preset.video-galaxy-android14.evidence.3',
          'url': 'https://github.com/immich-app/test-assets/blob/main/videos/waterfall.mp4'}],
        limitations=['preset.video-galaxy-android14.limit.1',
         'preset.video-galaxy-android14.limit.2',
         'preset.video-galaxy-android14.limit.3',
         'preset.video-galaxy-android14.limit.4'],
    ),
    _profile(
        id='builtin:video-premiere-2024',
        media='video',
        kind='editor',
        fields={'FFMPEG:container': 'mp4',
         'FFMPEG:codec': 'h264',
         'FFMPEG:strip_sei': '1',
         'XMP-x:XMPToolkit': 'Adobe XMP Core 9.0-c001 79.c0204b2, 2023/02/09-06:26:14',
         'XMP-xmp:CreatorTool': 'Adobe Premiere Pro 2024.0 (Windows)',
         'XMP-xmp:CreateDate': '$LOCAL_TZ',
         'XMP-xmp:ModifyDate': '$LOCAL_TZ',
         'XMP-xmp:MetadataDate': '$LOCAL_TZ',
         'XMP-xmpMM:DocumentID': '$XMP_DID',
         'XMP-xmpMM:InstanceID': '$XMP_IID',
         'XMP-xmpMM:OriginalDocumentID': '$XMP_DID',
         'XMP-xmpMM:HistorySoftwareAgent': 'Adobe Premiere Pro 2024.0 (Windows)',
         'XMP-xmpDM:VideoFrameRate': '$FPS',
         'XMP-xmpDM:VideoFieldOrder': 'Progressive',
         'XMP-xmpDM:VideoPixelAspectRatio': '1/1',
         'XMP-xmpDM:StartTimecodeTimeValue': '00:00:00:00'},
        name_key='preset.video-premiere-2024.name',
        summary_key='preset.video-premiere-2024.summary',
        group='group.adobe_premiere',
        evidence=[{'key': 'preset.video-premiere-2024.evidence.1',
          'url': 'https://github.com/revolutioncom123/4k_wall/blob/main/Motion%20Array.webm.xmp'},
         {'key': 'preset.video-premiere-2024.evidence.2',
          'url': 'https://creativecow.net/forums/thread/careful-now-premiere-saves-all-your-project-info-i/'}],
        limitations=['preset.video-premiere-2024.limit.1',
         'preset.video-premiere-2024.limit.2',
         'preset.video-premiere-2024.limit.3',
         'preset.video-premiere-2024.limit.4'],
    ),
    _profile(
        id='builtin:video-resolve-studio',
        media='video',
        kind='editor',
        fields={'FFMPEG:container': 'mp4',
         'FFMPEG:codec': 'h264',
         'FFMPEG:strip_sei': '1',
         'FFMPEG:encoder': 'Blackmagic Design DaVinci Resolve Studio',
         'FFMPEG:timecode': '01:00:00:00'},
        name_key='preset.video-resolve-studio.name',
        summary_key='preset.video-resolve-studio.summary',
        group='group.davinci_resolve',
        evidence=[{'key': 'preset.video-resolve-studio.evidence.1',
          'url': 'https://forum.blackmagicdesign.com/viewtopic.php?t=175315'},
         {'key': 'preset.video-resolve-studio.evidence.2',
          'url': 'https://forum.grassvalley.com/forum/editors/editing-with-edius/570922-no-audio-in-edius-8-for-clip-exported-from-davinci-resolve-17'}],
        limitations=['preset.video-resolve-studio.limit.1',
         'preset.video-resolve-studio.limit.2',
         'preset.video-resolve-studio.limit.3'],
    ),
    _profile(
        id='builtin:video-resolve-free',
        media='video',
        kind='editor',
        fields={'FFMPEG:container': 'mp4',
         'FFMPEG:codec': 'h264',
         'FFMPEG:strip_sei': '1',
         'FFMPEG:encoder': 'Blackmagic Design DaVinci Resolve',
         'FFMPEG:timecode': '01:00:00:00'},
        name_key='preset.video-resolve-free.name',
        summary_key='preset.video-resolve-free.summary',
        group='group.davinci_resolve',
        evidence=[{'key': 'preset.video-resolve-free.evidence.1',
          'url': 'https://forum.grassvalley.com/forum/editors/editing-with-edius/570922-no-audio-in-edius-8-for-clip-exported-from-davinci-resolve-17'}],
        limitations=['preset.video-resolve-free.limit.1', 'preset.video-resolve-free.limit.2'],
    ),
    _profile(
        id='builtin:video-capcut',
        media='video',
        kind='editor',
        available=False,
        fields={},
        name_key='preset.video-capcut.name',
        summary_key='preset.video-capcut.summary',
        group='group.capcut',
        evidence=[{'key': 'preset.video-capcut.evidence.1'}],
        limitations=['preset.video-capcut.limit.1'],
    ),
]

BUILTIN_INDEX = {profile["id"]: profile for profile in BUILTIN_PROFILES}


def builtin_profile(profile_id: str) -> dict | None:
    profile = BUILTIN_INDEX.get(profile_id)
    return copy.deepcopy(profile) if profile else None


def default_profile_id(media: str) -> str:
    return f"builtin:{media}-none"


def profile_name(profile: dict) -> str:
    return tr(profile["name_key"]) if profile.get("name_key") else profile.get("name", "")


def profile_summary(profile: dict) -> str:
    return tr(profile["summary_key"]) if profile.get("summary_key") else profile.get("summary", "")


def profile_group(profile: dict) -> str:
    group = profile.get("group", "group.custom")
    return tr(group) if group.startswith("group.") else group


class ProfileStore:
    """Saved metadata profiles: user copies of presets and non-destructive edits of built-ins.

    Built-in definitions above are never modified; an edited built-in is stored as an
    override that "reset" simply deletes.
    """

    def __init__(self, path):
        import json
        from pathlib import Path

        self.path = Path(path)
        self.data = {"custom": {}, "overrides": {}, "active": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            for key in self.data:
                if isinstance(loaded.get(key), dict):
                    self.data[key] = loaded[key]
        except (OSError, ValueError):
            pass

    def _write(self) -> None:
        import json

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def profiles(self, media: str) -> list[dict]:
        result = [self.get(profile["id"]) for profile in BUILTIN_PROFILES if profile["media"] == media]
        result += [copy.deepcopy(profile) for profile in self.data["custom"].values() if profile.get("media") == media]
        return result

    def get(self, profile_id: str) -> dict | None:
        if profile_id in self.data["custom"]:
            return copy.deepcopy(self.data["custom"][profile_id])
        if profile_id in self.data["overrides"] and profile_id in BUILTIN_INDEX:
            profile = copy.deepcopy(self.data["overrides"][profile_id])
            profile["modified"] = True
            return profile
        return builtin_profile(profile_id)

    def is_modified(self, profile_id: str) -> bool:
        return profile_id in self.data["overrides"]

    def save(self, profile: dict) -> None:
        profile = copy.deepcopy(profile)
        profile.pop("modified", None)
        if profile.get("builtin"):
            if profile == BUILTIN_INDEX.get(profile["id"]):
                self.data["overrides"].pop(profile["id"], None)
            else:
                self.data["overrides"][profile["id"]] = profile
        else:
            self.data["custom"][profile["id"]] = profile
        self._write()

    def reset(self, profile_id: str) -> dict | None:
        self.data["overrides"].pop(profile_id, None)
        self._write()
        return builtin_profile(profile_id)

    def save_as(self, profile: dict, name: str) -> dict:
        import uuid

        new = copy.deepcopy(profile)
        new.pop("modified", None)
        new.pop("name_key", None)
        # Keys (summary, sources, limitations, base preset name) stay i18n keys so a saved
        # profile follows the interface language; only the user's own name is literal.
        new.update(id=f"custom:{uuid.uuid4().hex[:12]}", name=name, builtin=False, available=True,
                   group="group.custom", based_on=profile_name(profile), based_on_key=profile.get("name_key", ""))
        self.data["custom"][new["id"]] = new
        self._write()
        return copy.deepcopy(new)

    def delete(self, profile_id: str) -> None:
        self.data["custom"].pop(profile_id, None)
        for media, active in list(self.data["active"].items()):
            if active == profile_id:
                self.data["active"][media] = default_profile_id(media)
        self._write()

    def active_id(self, media: str) -> str:
        profile_id = self.data["active"].get(media, default_profile_id(media))
        profile = self.get(profile_id)
        return profile_id if profile and profile.get("available", True) else default_profile_id(media)

    def set_active(self, media: str, profile_id: str) -> None:
        self.data["active"][media] = profile_id
        self._write()
