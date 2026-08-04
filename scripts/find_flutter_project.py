#!/usr/bin/env python3
"""Discover the most likely Flutter project in an extracted source tree.

Buildino v0.6.0 intentionally accepts incomplete and non-standard Flutter archives.
A candidate does not need an Android directory. Selection is deterministic and is
based on source signals rather than archive depth alone.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

IGNORED_DIRS = {
    ".git", ".dart_tool", "build", "node_modules", ".gradle", ".idea",
    ".pub-cache", "Pods", "DerivedData", "coverage",
}


@dataclass(frozen=True)
class Candidate:
    path: str
    relative_path: str
    depth: int
    score: int
    confidence: str
    has_flutter_sdk_dependency: bool
    has_lib: bool
    has_main_dart: bool
    has_android: bool
    has_ios: bool
    has_web: bool
    has_metadata: bool
    has_test: bool
    pubspec_name: str | None
    reasons: tuple[str, ...]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_pubspec_name(text: str) -> str | None:
    match = re.search(r"(?m)^name:\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:#.*)?$", text)
    return match.group(1) if match else None


def contains_flutter_sdk(text: str) -> bool:
    # Covers normal dependencies and dev_dependencies formatting without requiring PyYAML.
    return bool(re.search(r"(?ms)^\s*flutter\s*:\s*\n\s*sdk\s*:\s*['\"]?flutter['\"]?\s*$", text))


def score_candidate(root: Path, pubspec: Path) -> Candidate:
    project = pubspec.parent.resolve()
    text = read_text(pubspec)
    relative = project.relative_to(root.resolve())
    depth = len(relative.parts)
    has_flutter = contains_flutter_sdk(text)
    has_lib = (project / "lib").is_dir()
    has_main = (project / "lib/main.dart").is_file()
    has_android = (project / "android").is_dir()
    has_ios = (project / "ios").is_dir()
    has_web = (project / "web").is_dir()
    has_metadata = (project / ".metadata").is_file()
    has_test = (project / "test").is_dir()
    name = parse_pubspec_name(text)

    score = 0
    reasons: list[str] = []
    if has_flutter:
        score += 45
        reasons.append("pubspec references Flutter SDK")
    if has_main:
        score += 45
        reasons.append("lib/main.dart exists")
    elif has_lib:
        score += 22
        reasons.append("lib directory exists")
    if has_android:
        score += 24
        reasons.append("Android platform exists")
    if has_ios:
        score += 7
        reasons.append("iOS platform exists")
    if has_web:
        score += 4
        reasons.append("Web platform exists")
    if has_metadata:
        score += 8
        reasons.append("Flutter metadata exists")
    if has_test:
        score += 3
        reasons.append("test directory exists")
    if name:
        score += 3
        reasons.append("valid pubspec project name")
    if (project / "analysis_options.yaml").is_file():
        score += 2
        reasons.append("analysis options exist")
    if any((project / value).is_dir() for value in ("tooling", "android_overlay", "platform")):
        score += 3
        reasons.append("platform tooling detected")

    # Prefer shallower candidates only after stronger semantic signals.
    score -= min(depth, 12)
    confidence = "high" if score >= 80 else "medium" if score >= 55 else "low"
    return Candidate(
        path=str(project),
        relative_path=str(relative) if str(relative) != "." else ".",
        depth=depth,
        score=score,
        confidence=confidence,
        has_flutter_sdk_dependency=has_flutter,
        has_lib=has_lib,
        has_main_dart=has_main,
        has_android=has_android,
        has_ios=has_ios,
        has_web=has_web,
        has_metadata=has_metadata,
        has_test=has_test,
        pubspec_name=name,
        reasons=tuple(reasons),
    )


def find_candidates(root: Path) -> list[Candidate]:
    root = root.resolve()
    candidates: list[Candidate] = []
    for pubspec in root.rglob("pubspec.yaml"):
        try:
            relative_parts = pubspec.relative_to(root).parts
        except ValueError:
            continue
        if any(part in IGNORED_DIRS for part in relative_parts):
            continue
        candidate = score_candidate(root, pubspec)
        # Accept incomplete Flutter sources if either the SDK marker or actual Dart app source exists.
        if candidate.has_flutter_sdk_dependency or candidate.has_main_dart or candidate.has_lib:
            candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda item: (-item.score, item.depth, item.relative_path.casefold()),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    candidates = find_candidates(root)
    if not candidates:
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({
                "schema": 2,
                "root": str(root),
                "selected": None,
                "candidates": [],
                "error": "No pubspec.yaml with Flutter/Dart application signals was found",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("No Flutter or Dart application project candidate was found", file=sys.stderr)
        return 3

    selected = candidates[0]
    report = {
        "schema": 2,
        "root": str(root),
        "selected": asdict(selected),
        "candidate_count": len(candidates),
        "ambiguous": len(candidates) > 1 and candidates[1].score >= selected.score - 5,
        "candidates": [asdict(item) for item in candidates[:20]],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(selected.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
