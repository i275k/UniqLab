"""Scan source files or built packages for personal data before publishing.

Flags home-folder paths of real users (anything except CI runner accounts), e-mail
addresses in UniqLab's own files, and any extra strings passed with --forbid (for
example your local user name). Third-party license texts are allowed to contain e-mail
addresses; home paths are never allowed anywhere.

Usage:
  python tools/privacy_check.py PATH [PATH ...] [--forbid TEXT ...]
Exit code 1 when something was found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Account names that appear in upstream binaries and are not personal data: CI runners,
# Qt's build machines ("qt"), the build account of GitHub's macOS Python builds ("sysadmin",
# used by actions/setup-python), and the "/Users/Barney" example in Python's standard library.
ALLOWED_ACCOUNTS = {b"runner", b"runneradmin", b"shared", b"qt", b"barney", b"user", b"public", b"default",
                    b"linuxbrew", b"builder", b"builduser", b"sysadmin"}
HOME_PATTERNS = [
    re.compile(rb"/Users/([A-Za-z0-9._-]{2,})"),
    re.compile(rb"/home/([A-Za-z0-9._-]{2,})"),
    re.compile(rb"[A-Za-z]:\\{1,2}Users\\{1,2}([A-Za-z0-9._-]{2,})", re.IGNORECASE),
]
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
OWN_TEXT_SUFFIXES = {".py", ".json", ".md", ".toml", ".txt", ".yml", ".yaml", ".sh", ".ps1", ".iss", ".desktop", ".xml", ".spec"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "build", "dist", ".venv", "venv"}
THIRD_PARTY_HINTS = ("licenses", "LICENSE", "COPYING", "site-packages", "PySide6", "shiboken6", "PIL", "certifi",
                     "Python.framework", "_internal", "Frameworks", "lib-dynload", "THIRD_PARTY")


def iter_files(paths: list[Path]):
    for root in paths:
        if root.is_file():
            yield root
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and not (SKIP_DIRS & set(path.parts)):
                yield path


def scan(paths: list[Path], forbid: list[str]) -> list[str]:
    problems = []
    extra = [re.compile(re.escape(text.encode()), re.IGNORECASE) for text in forbid if text]
    for path in iter_files(paths):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        found = set()
        for pattern in HOME_PATTERNS:
            for match in pattern.finditer(data):
                if match.group(1).lower() not in ALLOWED_ACCOUNTS:
                    found.add(match.group(0)[:60].decode(errors="replace"))
        for pattern in extra:
            match = pattern.search(data)
            if match:
                found.add(match.group(0).decode(errors="replace"))
        problems += [f"{path}: {item}" for item in sorted(found)]
        own = path.suffix in OWN_TEXT_SUFFIXES and not any(hint in str(path) for hint in THIRD_PARTY_HINTS)
        if own:
            for match in EMAIL.finditer(data):
                address = match.group(0).decode(errors="replace")
                if not address.endswith("@example.com"):
                    problems.append(f"{path}: e-mail address {address}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--forbid", action="append", default=[], help="extra text that must not appear")
    args = parser.parse_args()
    problems = scan(args.paths, args.forbid)
    for line in problems:
        print(line)
    print(f"privacy check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
