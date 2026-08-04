#!/usr/bin/env python3
"""Validate an uploaded source ZIP while allowing common project archive layouts."""

from __future__ import annotations

import argparse
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1536 * 1024 * 1024
MAX_FILES = 50_000
MAX_COMPRESSION_RATIO = 250


class ZipValidationError(RuntimeError):
    pass


def normalized_member(name: str) -> PurePosixPath:
    if "\x00" in name:
        raise ZipValidationError("ZIP member contains a NUL byte")
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ZipValidationError(f"Unsafe ZIP path: {name!r}")
    return path


def is_symbolic_link(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def validate_zip(path: Path) -> tuple[int, int]:
    if not path.is_file():
        raise ZipValidationError(f"Archive not found: {path}")
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ZipValidationError("Compressed archive is larger than 500 MB")

    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ZipValidationError("Input is not a valid ZIP archive") from exc

    total_uncompressed = 0
    file_count = 0
    symlink_count = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise ZipValidationError(f"ZIP contains more than {MAX_FILES} entries")

        for info in infos:
            normalized_member(info.filename)
            if info.flag_bits & 0x1:
                raise ZipValidationError(f"Encrypted ZIP entries are not supported: {info.filename}")
            if info.is_dir():
                continue

            file_count += 1
            if is_symbolic_link(info):
                symlink_count += 1
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ZipValidationError("Total uncompressed size exceeds 1.5 GB")

            if info.file_size > 0:
                if info.compress_size == 0:
                    raise ZipValidationError(f"Suspicious compression metadata: {info.filename}")
                ratio = info.file_size / info.compress_size
                if ratio > MAX_COMPRESSION_RATIO:
                    raise ZipValidationError(
                        f"Compression ratio is too high ({ratio:.1f}x): {info.filename}"
                    )

    if file_count == 0:
        raise ZipValidationError("ZIP archive is empty")
    if symlink_count:
        print(
            f"Buildino compatibility: {symlink_count} symbolic link entries will be converted to regular files.",
            file=sys.stderr,
        )
    return file_count, total_uncompressed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        files, total = validate_zip(args.archive)
    except ZipValidationError as exc:
        print(f"ZIP validation failed: {exc}", file=sys.stderr)
        return 2
    print(f"ZIP validation passed: {files} files, {total} uncompressed bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
