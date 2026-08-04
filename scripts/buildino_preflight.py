#!/usr/bin/env python3
"""Analyze a Flutter Android project and prepare safe temporary build inputs."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
from pathlib import Path


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def gradle_version(project: Path) -> tuple[int, int]:
    text = read_text(project / "android/gradle/wrapper/gradle-wrapper.properties")
    match = re.search(r"gradle-(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else (8, 0)


def required_java(project: Path) -> int:
    texts = []
    for pattern in ("android/**/*.gradle", "android/**/*.gradle.kts"):
        texts.extend(read_text(path) for path in project.glob(pattern))
    joined = "\n".join(texts)
    for version in (21, 17, 11):
        patterns = (
            rf"VERSION_{version}\b",
            rf"jvmTarget\s*=\s*['\"]{version}['\"]",
            rf"jvmToolchain\s*\(\s*{version}\s*\)",
            rf"JavaLanguageVersion\.of\s*\(\s*{version}\s*\)",
        )
        if any(re.search(pattern, joined) for pattern in patterns):
            return version
    major, minor = gradle_version(project)
    # Runtime compatibility for older Android projects. Source/target compatibility
    # set to 1.8 does not itself force JDK 8, so only the Gradle wrapper drives this.
    if (major, minor) < (5, 0):
        return 8
    if (major, minor) < (7, 3):
        return 11
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


def detect_flavors(project: Path) -> list[str]:
    app_gradles = [project / "android/app/build.gradle", project / "android/app/build.gradle.kts"]
    for path in app_gradles:
        text = read_text(path)
        block = balanced_block(text, "productFlavors")
        if not block:
            continue
        names: list[str] = []
        # Groovy: bazaar { ... }; Kotlin: create("bazaar") { ... }
        for name in re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\{", block):
            if name not in {"create", "maybeCreate", "all", "configureEach"} and name not in names:
                names.append(name)
        for name in re.findall(r"\b(?:create|maybeCreate)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", block):
            if name not in names:
                names.append(name)
        return names
    return []



def detect_android_versions(project: Path) -> dict[str, str | int | None]:
    texts = []
    for path in (
        project / "android/app/build.gradle",
        project / "android/app/build.gradle.kts",
        project / "android/build.gradle",
        project / "android/build.gradle.kts",
    ):
        texts.append(read_text(path))
    joined = "\n".join(texts)

    def first_int(patterns: tuple[str, ...]) -> int | None:
        for pattern in patterns:
            match = re.search(pattern, joined, re.I)
            if match:
                return int(match.group(1))
        return None

    compile_sdk = first_int((
        r"\bcompileSdk\s*=\s*(\d+)",
        r"\bcompileSdkVersion\s+(\d+)",
    ))
    min_sdk = first_int((
        r"\bminSdk\s*=\s*(\d+)",
        r"\bminSdkVersion\s+(\d+)",
    ))
    target_sdk = first_int((
        r"\btargetSdk\s*=\s*(\d+)",
        r"\btargetSdkVersion\s+(\d+)",
    ))
    ndk_match = re.search(r"\bndkVersion\s*=\s*['\"]([^'\"]+)['\"]|\bndkVersion\s+['\"]([^'\"]+)['\"]", joined)
    build_tools_match = re.search(r"\bbuildToolsVersion\s*(?:=\s*)?['\"]([^'\"]+)['\"]", joined)
    cmake_match = re.search(r"\bcmake\s*\{[^{}]*\bversion\s*(?:=\s*)?['\"]([^'\"]+)['\"]", joined, re.S)
    return {
        "compile_sdk": compile_sdk,
        "min_sdk": min_sdk,
        "target_sdk": target_sdk,
        "ndk_version": next((value for value in (ndk_match.group(1), ndk_match.group(2)) if value), None) if ndk_match else None,
        "build_tools_version": build_tools_match.group(1) if build_tools_match else None,
        "cmake_version": cmake_match.group(1) if cmake_match else None,
    }


def detect_entrypoints(project: Path) -> list[str]:
    lib = project / "lib"
    if not lib.is_dir():
        return []
    preferred = []
    for path in sorted(lib.glob("main*.dart")):
        if path.is_file():
            preferred.append(str(path.relative_to(project)))
    if not preferred and (lib / "app.dart").is_file():
        preferred.append("lib/app.dart")
    return preferred


def parse_properties(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def existing_keystore(project: Path) -> tuple[bool, str]:
    android = project / "android"
    app_text = read_text(android / "app/build.gradle") + "\n" + read_text(android / "app/build.gradle.kts")
    references_properties = "key.properties" in app_text
    props_path = android / "key.properties"
    if not references_properties:
        embedded = [
            path for pattern in ("*.jks", "*.keystore")
            for path in android.rglob(pattern)
            if path.is_file() and path.stat().st_size > 0
        ]
        if embedded and re.search(r"signingConfigs|signingConfig", app_text):
            return True, "پروژه از تنظیم امضای اختصاصی غیرمبتنی بر key.properties استفاده می‌کند"
        return False, "امضای Release اختصاصی و قابل‌اعتبارسنجی شناسایی نشد"
    if not props_path.is_file():
        return False, "android/key.properties وجود ندارد"
    props = parse_properties(props_path)
    store = props.get("storeFile", "").strip()
    if not store:
        return False, "storeFile در android/key.properties خالی است"
    candidates = [
        (project / "android/app" / store).resolve(),
        (project / "android" / store).resolve(),
        (project / store).resolve(),
    ]
    if not any(candidate.is_file() and candidate.stat().st_size > 0 for candidate in candidates):
        return False, f"فایل Keystore معرفی‌شده پیدا نشد: {store}"
    required = ("storePassword", "keyPassword", "keyAlias")
    missing = [key for key in required if not props.get(key)]
    if missing:
        return False, "فیلدهای امضا ناقص‌اند: " + ", ".join(missing)
    return True, "امضای اختصاصی پروژه موجود است"


def ensure_local_properties(project: Path) -> None:
    android = project / "android"
    path = android / "local.properties"
    existing = parse_properties(path) if path.exists() else {}
    existing.setdefault("sdk.dir", os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or "/usr/local/lib/android/sdk")
    existing.setdefault("flutter.sdk", os.environ.get("FLUTTER_ROOT", ""))
    pubspec = read_text(project / "pubspec.yaml")
    version_match = re.search(r"(?m)^version:\s*([^+\s]+)(?:\+(\d+))?", pubspec)
    existing.setdefault("flutter.versionName", version_match.group(1) if version_match else "1.0.0")
    existing.setdefault("flutter.versionCode", version_match.group(2) if version_match and version_match.group(2) else "1")
    path.write_text("".join(f"{key}={value}\n" for key, value in existing.items()), encoding="utf-8")


def install_ephemeral_keystore(project: Path, java_home: str) -> dict[str, str]:
    android = project / "android"
    android.mkdir(parents=True, exist_ok=True)
    keystore = android / "buildino-ephemeral.jks"
    password = secrets.token_hex(18)
    alias = "buildino_ephemeral"
    keytool = Path(java_home) / "bin/keytool" if java_home else Path("keytool")
    command = [
        str(keytool), "-genkeypair", "-noprompt", "-keystore", str(keystore),
        "-storetype", "JKS", "-storepass", password, "-keypass", password,
        "-alias", alias, "-keyalg", "RSA", "-keysize", "2048",
        "-sigalg", "SHA256withRSA", "-validity", "30",
        "-dname", "CN=Buildino Ephemeral Build, OU=Temporary, O=Buildino",
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (android / "key.properties").write_text(
        "\n".join([
            f"storePassword={password}", f"keyPassword={password}",
            f"keyAlias={alias}", "storeFile=../buildino-ephemeral.jks", "",
        ]), encoding="utf-8"
    )
    return {"alias": alias, "store_file": "android/buildino-ephemeral.jks"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    java_version = required_java(project)
    java_home = os.environ.get(f"JAVA_HOME_{java_version}_X64", "") or os.environ.get("JAVA_HOME", "")
    if not java_home:
        raise SystemExit(f"JAVA_HOME for Java {java_version} is unavailable")
    ensure_local_properties(project)
    valid_signing, signing_reason = existing_keystore(project)
    fallback = not valid_signing
    ephemeral: dict[str, str] | None = None
    if fallback:
        ephemeral = install_ephemeral_keystore(project, java_home)
    android_versions = detect_android_versions(project)
    data = {
        "java_version": java_version,
        "java_home": java_home,
        "gradle_version": ".".join(map(str, gradle_version(project))),
        "flavors": detect_flavors(project),
        "entrypoints": detect_entrypoints(project),
        "fallback_signing_used": fallback,
        "signing_reason": signing_reason,
        "ephemeral_signing": ephemeral,
        **android_versions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
