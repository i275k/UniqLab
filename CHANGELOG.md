# Changelog

All notable changes to UniqLab are listed here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-11-27

First public release.

### Added
- Batch video processing for MP4, MOV and AVI: random 0.05–0.15 s trims, light temporal noise,
  a 1–2 % brightness or contrast shift, a new random file name and a center crop to 10 aspect
  ratios or a random 2–10 % crop. H.264 (MP4) by default, HEVC (MOV/MP4) with some presets.
- Batch photo processing for JPEG, PNG and WebP with Light, Medium and Strong settings; format,
  dimensions, transparency, color profile and orientation are kept.
- Metadata page: source metadata removed by default or kept and extended; built-in presets for
  iPhone 12 Pro, Samsung Galaxy, Premiere Pro 2024 and DaVinci Resolve (video) and iPhone 15 Pro,
  iPhone 16 Pro, Galaxy S25 Ultra and Galaxy S24 (photo), each with sources and limitations;
  read-back verification of every written field; manual editing and custom presets.
  GPS and location data are never added.
- Component manager for FFmpeg and ExifTool: detection, one-click download of pinned builds with
  SHA-256 verification, resumable downloads, no administrator rights, update check on request.
- Interface in English, Simplified Chinese, Portuguese (Brazil), Spanish and Russian with live
  switching; light, dark and automatic themes; reduced-motion support.
- Packages: macOS DMG (Apple silicon), Windows x64 per-user installer, Linux x64 AppImage, with
  stable file names and `SHA256SUMS.txt`.

### Known limitations
- CPU encoding only; no Intel Mac, Windows on ARM or Linux on ARM builds.
- Packages are not signed with paid certificates.
- Hands-on testing on Windows and Linux desktops has not been done yet.

[1.0.0]: https://github.com/i275k/UniqLab/releases/tag/v1.0.0
