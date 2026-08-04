#!/usr/bin/env python3
"""Adapt a discovered Flutter source into a buildable Android workspace.

This script never edits the uploaded ZIP. It operates only inside the temporary
GitHub Actions workspace, can generate a missing Android platform, and merges a
project-provided Android overlay when one is available.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

TEXT_SUFFIXES = {".sh", ".gradle", ".kts", ".properties", ".xml", ".yaml", ".yml", ".dart", ".java", ".kt"}
EXPLICIT_OVERLAYS = (
    "tooling/android_overlay",
    "android_overlay",
    "tooling/android",
    "platform/android",
    "platforms/android",
    "templates/android",
    "android-template",
    "android_template",
    "android_files",
)


@dataclass(frozen=True)
class OverlayCandidate:
    path: Path
    score: int
    reasons: tuple[str, ...]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_project_name(project: Path) -> str:
    match = re.search(r"(?m)^name:\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:#.*)?$", read_text(project / "pubspec.yaml"))
    if match:
        return match.group(1)
    raw = re.sub(r"[^A-Za-z0-9_]+", "_", project.name.lower()).strip("_")
    if not raw or not raw[0].isalpha():
        raw = f"buildino_{raw or 'app'}"
    return raw[:50]


def detect_application_id(root: Path) -> str | None:
    patterns = (
        r"\bapplicationId\s*(?:=\s*)?['\"]([A-Za-z][A-Za-z0-9_.]+)['\"]",
        r"\bnamespace\s*(?:=\s*)?['\"]([A-Za-z][A-Za-z0-9_.]+)['\"]",
        r"\bpackage\s*=\s*['\"]([A-Za-z][A-Za-z0-9_.]+)['\"]",
    )
    files = list(root.rglob("build.gradle")) + list(root.rglob("build.gradle.kts")) + list(root.rglob("AndroidManifest.xml"))
    for path in files[:100]:
        text = read_text(path)
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
    return None


def derive_org(application_id: str | None) -> str:
    if application_id:
        parts = [part for part in application_id.split(".") if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", part)]
        if len(parts) >= 2:
            return ".".join(parts[:-1])
    return "com.buildino.generated"


def android_complete(android: Path) -> bool:
    settings = (android / "settings.gradle").is_file() or (android / "settings.gradle.kts").is_file()
    app_gradle = (android / "app/build.gradle").is_file() or (android / "app/build.gradle.kts").is_file()
    wrapper = (android / "gradle/wrapper/gradle-wrapper.properties").is_file()
    manifest = (android / "app/src/main/AndroidManifest.xml").is_file()
    return settings and app_gradle and wrapper and manifest


def overlay_score(path: Path, project: Path) -> OverlayCandidate | None:
    try:
        relative = path.relative_to(project)
    except ValueError:
        return None
    if path == project / "android" or "build" in relative.parts or ".gradle" in relative.parts:
        return None
    score = 0
    reasons: list[str] = []
    name = path.name.casefold().replace("-", "_")
    rel_text = str(relative).casefold().replace("-", "_")
    if "android" in name or "android" in rel_text:
        score += 15
        reasons.append("android-named directory")
    if "overlay" in name or "overlay" in rel_text:
        score += 30
        reasons.append("overlay naming")
    if "template" in name or "template" in rel_text:
        score += 18
        reasons.append("template naming")
    checks = (
        ((path / "settings.gradle").is_file() or (path / "settings.gradle.kts").is_file(), 18, "Gradle settings"),
        ((path / "build.gradle").is_file() or (path / "build.gradle.kts").is_file(), 12, "root Gradle file"),
        ((path / "app/build.gradle").is_file() or (path / "app/build.gradle.kts").is_file(), 22, "Android app module"),
        ((path / "app/src/main/AndroidManifest.xml").is_file(), 22, "Android manifest"),
        ((path / "gradle/wrapper/gradle-wrapper.properties").is_file(), 8, "Gradle wrapper"),
    )
    for present, points, reason in checks:
        if present:
            score += points
            reasons.append(reason)
    if score < 30:
        return None
    return OverlayCandidate(path=path, score=score, reasons=tuple(reasons))


def find_overlay(project: Path) -> tuple[OverlayCandidate | None, list[OverlayCandidate]]:
    candidates: dict[Path, OverlayCandidate] = {}
    for relative in EXPLICIT_OVERLAYS:
        path = project / relative
        if path.is_dir():
            candidate = overlay_score(path, project)
            if candidate:
                # Explicit conventional paths receive a deterministic bonus.
                candidate = OverlayCandidate(candidate.path, candidate.score + 40, candidate.reasons + ("known overlay path",))
                candidates[path.resolve()] = candidate
    for path in project.rglob("*"):
        if not path.is_dir():
            continue
        try:
            depth = len(path.relative_to(project).parts)
        except ValueError:
            continue
        if depth > 5:
            continue
        candidate = overlay_score(path, project)
        if candidate:
            current = candidates.get(path.resolve())
            if current is None or candidate.score > current.score:
                candidates[path.resolve()] = candidate
    ordered = sorted(candidates.values(), key=lambda item: (-item.score, len(item.path.parts), str(item.path)))
    return (ordered[0] if ordered else None), ordered


def copy_tree(source: Path, destination: Path) -> int:
    count = 0
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_symlink():
            continue
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def normalize_workspace(project: Path) -> list[str]:
    actions: list[str] = []
    normalized = 0
    for path in project.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"gradlew", "gradlew.bat"}:
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\r\n" in raw:
                path.write_bytes(raw.replace(b"\r\n", b"\n"))
                normalized += 1
    if normalized:
        actions.append(f"normalized line endings in {normalized} files")
    for path in (project / "android/gradlew",):
        if path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            actions.append("restored executable permission for android/gradlew")
    for path in project.rglob("*.sh"):
        if path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode | stat.S_IXUSR)
    return actions


def run_flutter_create(project: Path, project_name: str, org: str, logs: list[dict]) -> str:
    direct = [
        "flutter", "create", "--platforms=android", "--no-pub",
        "--project-name", project_name, "--org", org, ".",
    ]
    completed = subprocess.run(direct, cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    logs.append({"strategy": "in_place", "command": direct, "exit_code": completed.returncode, "output_tail": completed.stdout[-2000:]})
    if completed.returncode == 0 and android_complete(project / "android"):
        return "in_place"

    # Fallback: create a clean seed project and copy only its generated Android platform.
    with tempfile.TemporaryDirectory(prefix="buildino-flutter-seed-") as temp:
        seed = Path(temp) / "seed"
        command = [
            "flutter", "create", "--platforms=android", "--no-pub",
            "--project-name", project_name, "--org", org, str(seed),
        ]
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        logs.append({"strategy": "seed_copy", "command": command, "exit_code": completed.returncode, "output_tail": completed.stdout[-2000:]})
        if completed.returncode != 0 or not android_complete(seed / "android"):
            raise RuntimeError("Flutter could not generate a complete Android platform")
        if (project / "android").exists():
            shutil.rmtree(project / "android")
        shutil.copytree(seed / "android", project / "android")
    return "seed_copy"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    if not (project / "pubspec.yaml").is_file():
        raise SystemExit("Selected project does not contain pubspec.yaml")
    if not (project / "lib").is_dir():
        raise SystemExit("Selected project does not contain a lib directory")

    android = project / "android"
    initial_android_exists = android.is_dir()
    initial_android_complete = android_complete(android)
    selected_overlay, overlay_candidates = find_overlay(project)
    project_name = parse_project_name(project)
    application_id = detect_application_id(selected_overlay.path if selected_overlay else project)
    org = derive_org(application_id)
    actions: list[str] = []
    generation_logs: list[dict] = []
    generated = False
    merged_overlay = False
    merged_files = 0
    strategy: str | None = None

    with tempfile.TemporaryDirectory(prefix="buildino-platform-backup-") as temp:
        temp_root = Path(temp)
        overlay_backup: Path | None = None
        existing_android_backup: Path | None = None
        if selected_overlay:
            overlay_backup = temp_root / "overlay"
            shutil.copytree(selected_overlay.path, overlay_backup)
        if initial_android_exists and not initial_android_complete:
            existing_android_backup = temp_root / "existing-android"
            shutil.copytree(android, existing_android_backup)

        if not initial_android_complete:
            strategy = run_flutter_create(project, project_name, org, generation_logs)
            generated = True
            actions.append(
                "generated missing/incomplete Android platform with Flutter "
                f"({strategy.replace('_', ' ')})"
            )
            if existing_android_backup:
                merged_files += copy_tree(existing_android_backup, android)
                actions.append("merged original incomplete android directory over generated platform")
        # For a complete standard Android project, merge only an explicitly named/known
        # overlay. Structural candidates are used automatically when Android was missing
        # or incomplete, but never overwrite a complete project merely because another
        # Android sample exists elsewhere in the archive.
        overlay_is_explicit = bool(selected_overlay and (
            "known overlay path" in selected_overlay.reasons or
            "overlay naming" in selected_overlay.reasons
        ))
        if overlay_backup and (not initial_android_complete or overlay_is_explicit):
            merged_files += copy_tree(overlay_backup, android)
            merged_overlay = True
            actions.append(f"applied Android overlay from {selected_overlay.path.relative_to(project)}")
        elif overlay_backup:
            actions.append(
                f"ignored non-explicit Android candidate {selected_overlay.path.relative_to(project)} "
                "because the existing Android platform was already complete"
            )

    actions.extend(normalize_workspace(project))
    final_complete = android_complete(android)
    if not final_complete:
        report = {
            "schema": 2,
            "status": "failure",
            "project_dir": str(project),
            "project_name": project_name,
            "initial_android_exists": initial_android_exists,
            "initial_android_complete": initial_android_complete,
            "android_generated": generated,
            "generation_strategy": strategy,
            "overlay_path": str(selected_overlay.path.relative_to(project)) if selected_overlay else None,
            "overlay_merged": merged_overlay,
            "merged_files": merged_files,
            "actions": actions,
            "generation_attempts": generation_logs,
            "error": "Android platform remains incomplete after adaptive preparation",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 31

    mode = "standard"
    if generated and merged_overlay:
        mode = "generated_android_with_overlay"
    elif generated:
        mode = "generated_android"
    elif merged_overlay:
        mode = "existing_android_with_overlay"

    report = {
        "schema": 2,
        "status": "success",
        "project_dir": str(project),
        "project_name": project_name,
        "application_id_hint": application_id,
        "generated_org": org,
        "mode": mode,
        "initial_android_exists": initial_android_exists,
        "initial_android_complete": initial_android_complete,
        "android_generated": generated,
        "generation_strategy": strategy,
        "overlay_path": str(selected_overlay.path.relative_to(project)) if selected_overlay else None,
        "overlay_merged": merged_overlay,
        "merged_files": merged_files,
        "actions": actions,
        "generation_attempts": generation_logs,
        "overlay_candidates": [
            {"path": str(item.path.relative_to(project)), "score": item.score, "reasons": list(item.reasons)}
            for item in overlay_candidates[:10]
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
