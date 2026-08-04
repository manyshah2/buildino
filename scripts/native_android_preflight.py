#!/usr/bin/env python3
"""Inspect a native Android Gradle project and select its application module."""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_gradle_version(root: Path) -> str | None:
    body = read_text(root / "gradle/wrapper/gradle-wrapper.properties")
    match = re.search(r"gradle-([0-9]+(?:\.[0-9]+){1,2})-(?:bin|all)\.zip", body)
    return match.group(1) if match else None


def detect_agp_version(root: Path) -> str | None:
    bodies = []
    for path in (
        root / "settings.gradle", root / "settings.gradle.kts",
        root / "build.gradle", root / "build.gradle.kts",
        root / "gradle/libs.versions.toml",
    ):
        bodies.append(read_text(path))
    joined = "\n".join(bodies)
    patterns = (
        r"com\.android\.application[\"']?\s+version\s+[\"']([^\"']+)",
        r"com\.android\.tools\.build:gradle:([^\"'\s)]+)",
        r"(?m)^\s*(?:agp|androidGradlePlugin)\s*=\s*[\"']([^\"']+)",
    )
    for pattern in patterns:
        match = re.search(pattern, joined)
        if match:
            return match.group(1)
    return None


def fallback_gradle_version(agp: str | None) -> str:
    if not agp:
        return "8.10.2"
    try:
        parts = tuple(int(x) for x in re.findall(r"\d+", agp)[:2])
    except ValueError:
        return "8.10.2"
    major, minor = (parts + (0, 0))[:2]
    if major >= 9:
        return "9.1.0"
    if major == 8:
        return {
            0: "8.0.2", 1: "8.0.2", 2: "8.2.1", 3: "8.4",
            4: "8.6", 5: "8.7", 6: "8.7", 7: "8.9",
            8: "8.10.2", 9: "8.11.1", 10: "8.11.1",
        }.get(minor, "8.10.2")
    if major == 7:
        return {0: "7.0.2", 1: "7.2", 2: "7.3.3", 3: "7.4.2", 4: "7.6.4"}.get(minor, "7.6.4")
    if major == 4:
        return "6.7.1"
    if major == 3:
        return "6.9.4" if minor >= 6 else "5.6.4"
    return "8.10.2"


def required_java(gradle_version: str | None, agp: str | None, bodies: str) -> int:
    for version in (21, 17, 11, 8):
        patterns = (
            rf"VERSION_{version}\b",
            rf"jvmTarget\s*=\s*[\"']{version}[\"']",
            rf"jvmToolchain\s*\(\s*{version}\s*\)",
            rf"JavaLanguageVersion\.of\s*\(\s*{version}\s*\)",
        )
        if any(re.search(pattern, bodies) for pattern in patterns):
            return version
    if agp:
        nums = [int(x) for x in re.findall(r"\d+", agp)[:2]]
        if nums:
            major = nums[0]
            if major >= 9:
                return 17
            if major >= 8:
                return 17
            if major == 7:
                return 11
    if gradle_version:
        nums = [int(x) for x in re.findall(r"\d+", gradle_version)[:2]]
        if nums:
            major, minor = (nums + [0])[:2]
            if (major, minor) >= (8, 5):
                return 17
            if (major, minor) >= (7, 3):
                return 17
            if major >= 5:
                return 11
            return 8
    return 17


def balanced_block(text: str, keyword: str) -> str:
    match = re.search(rf"\b{re.escape(keyword)}\b\s*(?:\([^)]*\))?\s*\{{", text)
    if not match:
        return ""
    start = text.find("{", match.start())
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
    return ""


def detect_flavors(module_gradle: str) -> list[str]:
    block = balanced_block(module_gradle, "productFlavors")
    if not block:
        return []
    names: list[str] = []
    for name in re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\{", block):
        if name not in {"create", "maybeCreate", "all", "configureEach"} and name not in names:
            names.append(name)
    for name in re.findall(r"\b(?:create|maybeCreate|register)\s*\(\s*[\"']([^\"']+)[\"']\s*\)", block):
        if name not in names:
            names.append(name)
    return names


def detect_android_versions(bodies: str) -> dict[str, str | int | None]:
    def first_int(patterns: tuple[str, ...]) -> int | None:
        for pattern in patterns:
            match = re.search(pattern, bodies, re.I)
            if match:
                return int(match.group(1))
        return None

    ndk_match = re.search(r"\bndkVersion\s*(?:=\s*)?[\"']([^\"']+)[\"']", bodies)
    build_tools_match = re.search(r"\bbuildToolsVersion\s*(?:=\s*)?[\"']([^\"']+)[\"']", bodies)
    cmake_match = re.search(r"\bcmake\s*\{[^{}]*\bversion\s*(?:=\s*)?[\"']([^\"']+)[\"']", bodies, re.S)
    return {
        "compile_sdk": first_int((r"\bcompileSdk\s*=\s*(\d+)", r"\bcompileSdkVersion\s+(\d+)")),
        "min_sdk": first_int((r"\bminSdk\s*=\s*(\d+)", r"\bminSdkVersion\s+(\d+)")),
        "target_sdk": first_int((r"\btargetSdk\s*=\s*(\d+)", r"\btargetSdkVersion\s+(\d+)")),
        "ndk_version": ndk_match.group(1) if ndk_match else None,
        "build_tools_version": build_tools_match.group(1) if build_tools_match else None,
        "cmake_version": cmake_match.group(1) if cmake_match else None,
    }


@dataclass(frozen=True)
class ModuleCandidate:
    path: str
    module_path: str
    score: int
    reasons: tuple[str, ...]


def is_application_plugin(body: str) -> bool:
    patterns = (
        r"\bid\s*\(?\s*[\"']com\.android\.application[\"']",
        r"\bapply\s+plugin\s*:\s*[\"']com\.android\.application[\"']",
        r"\balias\s*\(\s*libs\.plugins\.[A-Za-z0-9_.-]*(?:android\.application|application)[A-Za-z0-9_.-]*\s*\)",
    )
    return any(re.search(pattern, body) for pattern in patterns)


def module_path(root: Path, module: Path) -> str:
    rel = module.relative_to(root)
    return ":" + ":".join(rel.parts) if rel.parts else ""


def find_application_modules(root: Path) -> list[ModuleCandidate]:
    candidates: list[ModuleCandidate] = []
    gradles = [*root.rglob("build.gradle"), *root.rglob("build.gradle.kts")]
    for gradle in gradles:
        rel = gradle.relative_to(root)
        if any(part in {".gradle", "build", "node_modules", ".git"} for part in rel.parts):
            continue
        body = read_text(gradle)
        if not is_application_plugin(body):
            continue
        module = gradle.parent
        score = 70
        reasons = ["com.android.application plugin"]
        if module.name == "app":
            score += 20; reasons.append("conventional app module")
        if (module / "src/main/AndroidManifest.xml").is_file():
            score += 18; reasons.append("AndroidManifest.xml")
        if re.search(r"\bapplicationId\b", body):
            score += 8; reasons.append("applicationId")
        if any((module / "src/main").rglob("*.kt")):
            score += 4; reasons.append("Kotlin sources")
        if any((module / "src/main").rglob("*.java")):
            score += 4; reasons.append("Java sources")
        candidates.append(ModuleCandidate(str(module.resolve()), module_path(root, module), score, tuple(reasons)))
    return sorted(candidates, key=lambda item: (-item.score, item.module_path.casefold()))


def ensure_local_properties(root: Path) -> None:
    path = root / "local.properties"
    current: dict[str, str] = {}
    for raw in read_text(path).splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, value = raw.split("=", 1)
            current[key.strip()] = value.strip()
    current["sdk.dir"] = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or "/usr/local/lib/android/sdk"
    path.write_text("".join(f"{key}={value}\n" for key, value in current.items()), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.project.resolve()
    settings = next((path for path in (root / "settings.gradle", root / "settings.gradle.kts") if path.is_file()), None)
    if not settings:
        raise SystemExit("Native Android project requires settings.gradle or settings.gradle.kts")
    modules = find_application_modules(root)
    if not modules:
        raise SystemExit("No Android application module using com.android.application was found")
    selected = modules[0]
    module = Path(selected.path)
    module_gradle_path = next((path for path in (module / "build.gradle", module / "build.gradle.kts") if path.is_file()), None)
    if not module_gradle_path:
        raise SystemExit("Selected Android application module has no build.gradle file")
    all_gradles = [*root.rglob("*.gradle"), *root.rglob("*.gradle.kts")]
    bodies = "\n".join(read_text(path) for path in all_gradles if not any(part in {".gradle", "build"} for part in path.relative_to(root).parts))
    wrapper_version = parse_gradle_version(root)
    agp_version = detect_agp_version(root)
    gradle_version = wrapper_version or fallback_gradle_version(agp_version)
    java_version = required_java(gradle_version, agp_version, bodies)
    java_home = os.environ.get(f"JAVA_HOME_{java_version}_X64", "") or os.environ.get("JAVA_HOME", "")
    if not java_home:
        raise SystemExit(f"JAVA_HOME for Java {java_version} is unavailable")
    ensure_local_properties(root)
    module_body = read_text(module_gradle_path)
    language = "kotlin" if any((module / "src/main").rglob("*.kt")) or "org.jetbrains.kotlin.android" in bodies or "kotlin-android" in bodies else "java"
    data = {
        "schema": 1,
        "project_dir": str(root),
        "settings_file": str(settings.relative_to(root)),
        "application_module": asdict(selected),
        "module_dir": str(module),
        "module_relative": str(module.relative_to(root)) if module != root else ".",
        "module_path": selected.module_path,
        "language": language,
        "java_version": java_version,
        "java_home": java_home,
        "gradle_wrapper_present": (root / "gradlew").is_file() and (root / "gradle/wrapper/gradle-wrapper.properties").is_file(),
        "gradle_version": gradle_version,
        "agp_version": agp_version,
        "flavors": detect_flavors(module_body),
        "fallback_signing_used": True,
        "signing_reason": "Native Android release output is signed in the isolated Buildino publication job.",
        "candidates": [asdict(item) for item in modules[:20]],
        **detect_android_versions(bodies),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
