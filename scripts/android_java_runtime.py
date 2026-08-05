#!/usr/bin/env python3
"""Detect and switch the Java runtime required by Android Gradle projects."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

SUPPORTED_JAVA = (8, 11, 17, 21)
ANDROID_PLUGIN_IDS = {"com.android.application", "com.android.library", "com.android.test"}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def numeric_version(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def gradle_wrapper_version(android_root: Path) -> str | None:
    body = read_text(android_root / "gradle/wrapper/gradle-wrapper.properties")
    match = re.search(r"gradle-([0-9]+(?:\.[0-9]+){1,2})-(?:bin|all)\.zip", body)
    return match.group(1) if match else None


def _version_catalog_agp(path: Path) -> str | None:
    if tomllib is None or not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    versions = data.get("versions", {}) if isinstance(data, dict) else {}
    plugins = data.get("plugins", {}) if isinstance(data, dict) else {}
    if not isinstance(versions, dict) or not isinstance(plugins, dict):
        return None
    for definition in plugins.values():
        if not isinstance(definition, dict) or definition.get("id") not in ANDROID_PLUGIN_IDS:
            continue
        direct = definition.get("version")
        if isinstance(direct, str):
            return direct
        if isinstance(direct, dict):
            ref = direct.get("ref")
            if isinstance(ref, str) and isinstance(versions.get(ref), str):
                return str(versions[ref])
        ref = definition.get("version.ref")
        if isinstance(ref, str) and isinstance(versions.get(ref), str):
            return str(versions[ref])
    return None


def detect_agp_version(android_root: Path) -> str | None:
    catalog = _version_catalog_agp(android_root / "gradle/libs.versions.toml")
    files = [
        android_root / "settings.gradle",
        android_root / "settings.gradle.kts",
        android_root / "build.gradle",
        android_root / "build.gradle.kts",
        android_root / "gradle.properties",
    ]
    joined = "\n".join(read_text(path) for path in files)

    direct_patterns = (
        # Kotlin/Groovy plugins DSL: id("com.android.application") version "8.8.2"
        r"id\s*\(\s*[\"']com\.android\.(?:application|library|test)[\"']\s*\)\s*version\s*[\"']([^\"']+)[\"']",
        r"id\s+[\"']com\.android\.(?:application|library|test)[\"']\s+version\s+[\"']([^\"']+)[\"']",
        # Legacy buildscript classpath.
        r"com\.android\.tools\.build:gradle:([^\"'\s)]+)",
        # Common explicit properties.
        r"(?m)^\s*(?:agp|agpVersion|androidGradlePlugin|androidGradlePluginVersion)\s*[=:]\s*[\"']?([0-9][^\"'\s]*)",
    )
    for pattern in direct_patterns:
        match = re.search(pattern, joined)
        if match:
            return match.group(1)

    # Resolve a simple variable used by the plugins DSL.
    variables: dict[str, str] = {}
    for name, value in re.findall(
        r"(?m)^\s*(?:val|var|def|ext\.)\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[\"']([^\"']+)[\"']",
        joined,
    ):
        variables[name] = value
    variable_match = re.search(
        r"id\s*\(\s*[\"']com\.android\.(?:application|library|test)[\"']\s*\)\s*version\s*([A-Za-z_][A-Za-z0-9_]*)",
        joined,
    )
    if variable_match and variable_match.group(1) in variables:
        return variables[variable_match.group(1)]
    return catalog


def _toolchain_requirement(bodies: str) -> int:
    requested = 0
    for pattern in (
        r"jvmToolchain\s*\(\s*(8|11|17|21)\s*\)",
        r"JavaLanguageVersion\.of\s*\(\s*(8|11|17|21)\s*\)",
        r"languageVersion\s*(?:=|\.set\()\s*JavaLanguageVersion\.of\s*\(\s*(8|11|17|21)\s*\)",
    ):
        for match in re.finditer(pattern, bodies):
            requested = max(requested, int(match.group(1)))
    return requested


def required_java_runtime(gradle_version: str | None, agp_version: str | None, bodies: str) -> int:
    """Select the Gradle runtime JDK, not the source/bytecode target JDK."""
    floor = 8
    agp = numeric_version(agp_version)
    if agp:
        if agp[0] >= 8:
            floor = max(floor, 17)
        elif agp[0] == 7:
            floor = max(floor, 11)

    gradle = numeric_version(gradle_version)
    if gradle:
        major, minor = (gradle + (0, 0))[:2]
        if major >= 9:
            floor = max(floor, 17)
        elif (major, minor) >= (7, 3):
            # Java 17 is the safest modern runtime when no stricter AGP marker exists.
            floor = max(floor, 17)
        elif major >= 5:
            floor = max(floor, 11)

    floor = max(floor, _toolchain_requirement(bodies))
    return next((version for version in SUPPORTED_JAVA if version >= floor), 21)


def required_java_from_log(text: str) -> int | None:
    patterns = (
        r"requires\s+(?:a\s+)?Java\s+(\d+)(?:\s+or\s+(?:newer|later|higher))?",
        r"minimum supported Gradle JVM version is\s+(\d+)",
        r"JVM\s+(\d+)\s+or\s+(?:newer|later|higher)",
        r"Java\s+(\d+)\s+or\s+(?:newer|later|higher)\s+is required",
        r"Android Gradle plugin requires Java\s+(\d+)",
    )
    found = 0
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            found = max(found, int(match.group(1)))
    if not found:
        return None
    return next((version for version in SUPPORTED_JAVA if version >= found), None)


def java_home(version: int) -> str:
    return os.environ.get(f"JAVA_HOME_{version}_X64", "") or os.environ.get(f"JAVA_HOME_{version}", "")


def update_preflight(path: Path, version: int, reason: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    home = java_home(version)
    if not home:
        raise SystemExit(f"JAVA_HOME for Java {version} is unavailable")
    previous = payload.get("java_version")
    payload["java_version"] = version
    payload["java_home"] = home
    retries = payload.setdefault("java_runtime_retries", [])
    if not isinstance(retries, list):
        retries = []
        payload["java_runtime_retries"] = retries
    retries.append({"from": previous, "to": version, "reason": reason})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    detect = sub.add_parser("detect")
    detect.add_argument("android_root", type=Path)

    log = sub.add_parser("required-from-log")
    log.add_argument("log", type=Path)

    apply = sub.add_parser("apply")
    apply.add_argument("preflight", type=Path)
    apply.add_argument("version", type=int, choices=SUPPORTED_JAVA)
    apply.add_argument("--reason", default="Gradle reported a newer Java runtime requirement")

    args = parser.parse_args()
    if args.command == "detect":
        root = args.android_root.resolve()
        bodies = "\n".join(
            read_text(path)
            for path in [*root.rglob("*.gradle"), *root.rglob("*.gradle.kts")]
            if not any(part in {".gradle", "build", ".git", "node_modules"} for part in path.relative_to(root).parts)
        )
        gradle = gradle_wrapper_version(root)
        agp = detect_agp_version(root)
        version = required_java_runtime(gradle, agp, bodies)
        print(json.dumps({"gradle_version": gradle, "agp_version": agp, "java_version": version}, separators=(",", ":")))
        return 0
    if args.command == "required-from-log":
        required = required_java_from_log(read_text(args.log))
        if required is None:
            return 1
        print(required)
        return 0
    payload = update_preflight(args.preflight, args.version, args.reason)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
