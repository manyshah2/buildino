#!/usr/bin/env python3
"""Apply deterministic Android/Gradle compatibility fixes after an exact failure.

The fixer is intentionally conservative: every edit requires a matching compiler or
Gradle diagnostic, is restricted to the temporary project workspace, and is attempted
at most once for each concrete output/flavor label.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def load_report(path: Path) -> dict:
    if not path.is_file():
        return {"schema": 2, "applied": [], "attempts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": 2, "applied": [], "attempts": []}
    if not isinstance(data, dict):
        return {"schema": 2, "applied": [], "attempts": []}
    data.setdefault("schema", 2)
    data.setdefault("applied", [])
    data.setdefault("attempts", [])
    return data


def app_gradle(project: Path) -> Path | None:
    for relative in ("android/app/build.gradle.kts", "android/app/build.gradle"):
        path = project / relative
        if path.is_file():
            return path
    return None


def derive_namespace(project: Path, text: str) -> str | None:
    patterns = (
        r"\bapplicationId\s*(?:=\s*)?['\"]([A-Za-z][A-Za-z0-9_.]+)['\"]",
        r"\bnamespace\s*(?:=\s*)?['\"]([A-Za-z][A-Za-z0-9_.]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    manifest = read_text(project / "android/app/src/main/AndroidManifest.xml")
    match = re.search(r"\bpackage\s*=\s*['\"]([A-Za-z][A-Za-z0-9_.]+)['\"]", manifest)
    if match:
        return match.group(1)
    for path in list((project / "android/app/src/main").rglob("*.kt")) + list((project / "android/app/src/main").rglob("*.java")):
        match = re.search(r"(?m)^\s*package\s+([A-Za-z][A-Za-z0-9_.]+)", read_text(path))
        if match:
            return match.group(1)
    return None


def insert_namespace(project: Path, log: str) -> dict | None:
    if not re.search(r"Namespace not specified|namespace.*not specified", log, re.I):
        return None
    path = app_gradle(project)
    if not path:
        return None
    text = read_text(path)
    if re.search(r"(?m)^\s*namespace\s*(?:=\s*)?['\"]", text):
        return None
    namespace = derive_namespace(project, text)
    if not namespace:
        return None
    marker = re.search(r"(?m)^\s*android\s*\{\s*$", text)
    if not marker:
        return None
    indent_match = re.match(r"\s*", marker.group(0))
    indent = (indent_match.group(0) if indent_match else "") + "    "
    syntax = f'{indent}namespace = "{namespace}"\n' if path.suffix == ".kts" else f'{indent}namespace "{namespace}"\n'
    updated = text[:marker.end()] + "\n" + syntax + text[marker.end():]
    path.write_text(updated, encoding="utf-8")
    return {"rule": "android_namespace_missing", "file": str(path.relative_to(project)), "before": "namespace missing", "after": namespace}


def update_numeric_property(text: str, names: tuple[str, ...], value: int) -> tuple[str, str | None]:
    for name in names:
        patterns = (
            re.compile(rf"(?m)^(?P<i>\s*){re.escape(name)}\s*=\s*(?:flutter\.[A-Za-z0-9_]+|\d+)\s*$"),
            re.compile(rf"(?m)^(?P<i>\s*){re.escape(name)}\s+(?:flutter\.[A-Za-z0-9_]+|\d+)\s*$"),
        )
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            replacement = f"{match.group('i')}{name} = {value}" if "=" in match.group(0) else f"{match.group('i')}{name} {value}"
            return text[:match.start()] + replacement + text[match.end():], match.group(0).strip()
    return text, None


def update_min_sdk(project: Path, log: str) -> dict | None:
    matches = re.findall(r"minSdkVersion\s+\d+\s+cannot be smaller than version\s+(\d+)", log, re.I)
    if not matches:
        matches = re.findall(r"uses-sdk:minSdkVersion\s+['\"]?(\d+)['\"]?.*?smaller than version\s+['\"]?(\d+)", log, re.I | re.S)
        required = max((int(pair[1]) for pair in matches), default=0)
    else:
        required = max(map(int, matches))
    if not required:
        return None
    path = app_gradle(project)
    if not path:
        return None
    text = read_text(path)
    updated, before = update_numeric_property(text, ("minSdk", "minSdkVersion"), required)
    if before is None or updated == text:
        return None
    path.write_text(updated, encoding="utf-8")
    return {"rule": "android_min_sdk_requirement", "file": str(path.relative_to(project)), "before": before, "after": f"minSdk = {required}"}


def update_compile_sdk(project: Path, log: str) -> dict | None:
    versions = [int(value) for value in re.findall(r"compile against version\s+(\d+)\s+or later", log, re.I)]
    versions += [int(value) for value in re.findall(r"requires compileSdk(?:Version)?\s*(?:>=|of at least)?\s*(\d+)", log, re.I)]
    if not versions:
        return None
    required = max(versions)
    path = app_gradle(project)
    if not path:
        return None
    text = read_text(path)
    updated, before = update_numeric_property(text, ("compileSdk", "compileSdkVersion"), required)
    if before is None or updated == text:
        return None
    path.write_text(updated, encoding="utf-8")
    return {"rule": "android_compile_sdk_requirement", "file": str(path.relative_to(project)), "before": before, "after": f"compileSdk = {required}"}


def add_exported(project: Path, log: str) -> dict | None:
    if not re.search(r"android:exported needs to be explicitly specified", log, re.I):
        return None
    path = project / "android/app/src/main/AndroidManifest.xml"
    text = read_text(path)
    if not text:
        return None
    activity_pattern = re.compile(r"<activity\b(?P<attrs>[^>]*)>(?P<body>.*?)</activity>", re.I | re.S)
    for match in activity_pattern.finditer(text):
        attrs, body = match.group("attrs"), match.group("body")
        if "android.intent.action.MAIN" not in body or "android.intent.category.LAUNCHER" not in body:
            continue
        if "android:exported=" in attrs:
            return None
        replacement = "<activity" + attrs + '\n            android:exported="true">' + body + "</activity>"
        updated = text[:match.start()] + replacement + text[match.end():]
        path.write_text(updated, encoding="utf-8")
        return {"rule": "android_exported_launcher", "file": str(path.relative_to(project)), "before": "launcher activity without android:exported", "after": 'android:exported="true"'}
    return None


def align_jvm_targets(project: Path, log: str) -> dict | None:
    match = re.search(r"Inconsistent JVM-target compatibility.*?\((\d+)\).*?\((\d+)\)", log, re.I | re.S)
    if not match:
        return None
    target = max(int(match.group(1)), int(match.group(2)))
    path = app_gradle(project)
    if not path:
        return None
    text = read_text(path)
    original = text
    text = re.sub(r"jvmTarget\s*=\s*['\"]\d+['\"]", f'jvmTarget = "{target}"', text)
    text = re.sub(r"JvmTarget\.JVM_\d+", f"JvmTarget.JVM_{target}", text)
    text = re.sub(r"JavaVersion\.VERSION_\d+", f"JavaVersion.VERSION_{target}", text)
    if text == original:
        return None
    path.write_text(text, encoding="utf-8")
    return {"rule": "android_jvm_target_alignment", "file": str(path.relative_to(project)), "before": f"mixed JVM targets {match.group(1)}/{match.group(2)}", "after": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--build-label", required=True)
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    log = args.log.read_text(encoding="utf-8", errors="replace")
    report = load_report(args.output)
    if any(item.get("build_label") == args.build_label for item in report["attempts"]):
        print(json.dumps({"applied_count": 0, "retry_recommended": False, "reason": "already_attempted"}))
        return 0

    attempt = {"build_label": args.build_label, "applied_count": 0, "rules_checked": [], "skipped": []}
    fixers = (insert_namespace, update_min_sdk, update_compile_sdk, add_exported, align_jvm_targets)
    for fixer in fixers:
        attempt["rules_checked"].append(fixer.__name__)
        try:
            applied = fixer(project, log)
        except Exception as exc:  # Keep one bad migration from hiding the original build failure.
            attempt["skipped"].append({"rule": fixer.__name__, "reason": type(exc).__name__})
            continue
        if applied:
            applied["build_label"] = args.build_label
            report["applied"].append(applied)
            attempt["applied_count"] += 1

    report["attempts"].append(attempt)
    report["retry_recommended"] = attempt["applied_count"] > 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "applied_count": attempt["applied_count"],
        "retry_recommended": attempt["applied_count"] > 0,
        "build_label": args.build_label,
        "rules": [item["rule"] for item in report["applied"] if item.get("build_label") == args.build_label],
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
