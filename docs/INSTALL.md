# Installing UniqLab

UniqLab packages are built in GitHub Actions and are **not signed with paid certificates**
(no Apple Developer ID, no notarization, no Windows Authenticode). The operating system
therefore warns the first time you open the app. Check the download first:

```bash
shasum -a 256 UniqLab-*.dmg              # macOS
sha256sum UniqLab-*.AppImage             # Linux
certutil -hashfile UniqLab-1.0.0-windows-x64.exe SHA256 # Windows (Command Prompt)
```

Compare the result with the matching line in `SHA256SUMS.txt` from the same release.

## macOS (Apple silicon, macOS 13 or newer)

1. Open the DMG and drag **UniqLab** to **Applications**.
2. Open UniqLab. macOS reports that it cannot verify the developer — click **Done**.
3. Open **System Settings → Privacy & Security**, scroll to the message about UniqLab and
   click **Open Anyway**, then confirm with your password or Touch ID.
4. From now on UniqLab opens normally.

The app is signed ad hoc only so that macOS can check its integrity; it is not notarized.
If you prefer the command line, after copying the app you can remove the download
quarantine flag yourself:

```bash
xattr -dr com.apple.quarantine /Applications/UniqLab.app
```

## Windows 10/11 (x64)

1. Run `UniqLab-<version>-windows-x64.exe`.
2. Microsoft Defender SmartScreen may show “Windows protected your PC”. Click
   **More info → Run anyway**.
3. The installer works per user (`%LOCALAPPDATA%\Programs\UniqLab`) and does not need
   administrator rights. Uninstall from **Settings → Apps**.

## Linux (x86_64)

```bash
chmod +x UniqLab-<version>-linux-x64.AppImage
./UniqLab-<version>-linux-x64.AppImage
```

If your distribution has no FUSE 2 library, run it with
`--appimage-extract-and-run`, or install `libfuse2` (Ubuntu 22.04: `sudo apt install libfuse2`;
Ubuntu 24.04+: `sudo apt install libfuse2t64`). ExifTool needs Perl (`sudo apt install perl`
if it is missing). Qt needs the usual desktop libraries (`libegl1`, `libxkbcommon0`,
`libfontconfig1`), which desktop installations already have.

## First launch

If FFmpeg or ExifTool are missing, UniqLab offers to download them. Nothing is downloaded
until you agree. You can always manage them later in **Settings → Components** (click the
status pill in the header).
