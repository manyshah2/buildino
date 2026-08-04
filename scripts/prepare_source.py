#!/usr/bin/env python3
"""Extract source ZIP and convert symbolic-link entries to regular files."""

from __future__ import annotations

import argparse
import os
import posixpath
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

from validate_zip import ZipValidationError, is_symbolic_link, normalized_member, validate_zip


def safe_target(destination: Path, relative: PurePosixPath) -> Path:
    destination_root = destination.resolve()
    target = (destination / Path(*relative.parts)).resolve()
    if destination_root not in target.parents and target != destination_root:
        raise ZipValidationError(f"Unsafe extraction destination: {relative}")
    return target


def normalized_link_target(link_path: PurePosixPath, raw_target: str) -> PurePosixPath | None:
    candidate = raw_target.replace("\\", "/").strip().strip("\x00")
    if not candidate or candidate.startswith("/"):
        return None
    combined = posixpath.normpath(posixpath.join(str(link_path.parent), candidate))
    path = PurePosixPath(combined)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def extract_safely(archive_path: Path, destination: Path) -> None:
    validate_zip(archive_path)
    destination.mkdir(parents=True, exist_ok=True)
    symlinks: list[tuple[zipfile.ZipInfo, PurePosixPath, bytes]] = []

    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            relative = normalized_member(info.filename)
            target = safe_target(destination, relative)

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            payload = archive.read(info)
            if is_symbolic_link(info):
                symlinks.append((info, relative, payload))
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            mode = info.external_attr >> 16
            if mode:
                safe_mode = stat.S_IMODE(mode) & 0o777
                safe_mode &= ~0o6000
                os.chmod(target, safe_mode or 0o644)

    converted = 0
    fallback = 0
    for info, relative, payload in symlinks:
        target = safe_target(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        raw_target = payload.decode("utf-8", errors="replace")
        resolved = normalized_link_target(relative, raw_target)
        source = safe_target(destination, resolved) if resolved is not None else None
        if source is not None and source.is_file():
            shutil.copyfile(source, target)
            try:
                shutil.copymode(source, target)
            except OSError:
                os.chmod(target, 0o644)
            converted += 1
        else:
            # Broken/external links are retained as harmless regular text files so
            # they never block an otherwise buildable Android source archive.
            target.write_text(raw_target, encoding="utf-8", errors="replace")
            os.chmod(target, 0o644)
            fallback += 1

    if symlinks:
        print(
            f"Buildino compatibility: converted {converted} symbolic links and preserved {fallback} unresolved links as regular files.",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        extract_safely(args.archive, args.destination)
    except (ZipValidationError, zipfile.BadZipFile, OSError) as exc:
        print(f"Source extraction failed: {exc}", file=sys.stderr)
        return 2
    print(args.destination.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
