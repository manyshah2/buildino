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


def load_preflight(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def app_module_dir(project: Path, preflight: dict | None = None) -> Path | None:
    preflight = preflight or {}
    raw = preflight.get("module_dir")
    if isinstance(raw, str) and raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = project / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(project.resolve())
        except (OSError, ValueError):
            resolved = None
        if resolved and resolved.is_dir():
            return resolved
    for relative in ("android/app", "app"):
        candidate = project / relative
        if candidate.is_dir():
            return candidate
    return None


def app_gradle(project: Path, preflight: dict | None = None) -> Path | None:
    module = app_module_dir(project, preflight)
    if not module:
        return None
    for name in ("build.gradle.kts", "build.gradle"):
        path = module / name
        if path.is_file():
            return path
    return None


def derive_namespace(project: Path, text: str, preflight: dict | None = None) -> str | None:
    patterns = (
        r"\bapplicationId\s*(?:=\s*)?['\"]([A-Za-z][A-Za-z0-9_.]+)['\"]",
        r"\bnamespace\s*(?:=\s*)?['\"]([A-Za-z][A-Za-z0-9_.]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    module = app_module_dir(project, preflight)
    manifest = read_text(module / "src/main/AndroidManifest.xml") if module else ""
    match = re.search(r"\bpackage\s*=\s*['\"]([A-Za-z][A-Za-z0-9_.]+)['\"]", manifest)
    if match:
        return match.group(1)
    source_root = module / "src/main" if module else project / "android/app/src/main"
    for path in list(source_root.rglob("*.kt")) + list(source_root.rglob("*.java")):
        match = re.search(r"(?m)^\s*package\s+([A-Za-z][A-Za-z0-9_.]+)", read_text(path))
        if match:
            return match.group(1)
    return None


def insert_namespace(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    if not re.search(r"Namespace not specified|namespace.*not specified", log, re.I):
        return None
    path = app_gradle(project, preflight)
    if not path:
        return None
    text = read_text(path)
    if re.search(r"(?m)^\s*namespace\s*(?:=\s*)?['\"]", text):
        return None
    namespace = derive_namespace(project, text, preflight)
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


def migrate_manifest_package(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    if not re.search(r"Incorrect package=[\"'].*?[\"'] found in source AndroidManifest\.xml|Setting the namespace via the package attribute.*no longer supported", log, re.I | re.S):
        return None
    module = app_module_dir(project, preflight)
    gradle = app_gradle(project, preflight)
    if not module or not gradle:
        return None
    manifest = module / "src/main/AndroidManifest.xml"
    manifest_text = read_text(manifest)
    if not manifest_text:
        return None
    package_match = re.search(r"(<manifest\b[^>]*?)\s+package\s*=\s*([\"'])([A-Za-z][A-Za-z0-9_.]+)\2", manifest_text, re.I | re.S)
    if not package_match:
        return None
    namespace = package_match.group(3)
    gradle_text = read_text(gradle)
    changes: list[str] = []
    if not re.search(r"(?m)^\s*namespace\s*(?:=\s*)?[\"']", gradle_text):
        marker = re.search(r"(?m)^\s*android\s*\{\s*$", gradle_text)
        if not marker:
            return None
        indent_match = re.match(r"\s*", marker.group(0))
        indent = (indent_match.group(0) if indent_match else "") + "    "
        syntax = f'{indent}namespace = "{namespace}"\n' if gradle.suffix == ".kts" else f'{indent}namespace "{namespace}"\n'
        gradle_text = gradle_text[:marker.end()] + "\n" + syntax + gradle_text[marker.end():]
        gradle.write_text(gradle_text, encoding="utf-8")
        changes.append("namespace_added")
    updated_manifest = manifest_text[:package_match.start()] + package_match.group(1) + manifest_text[package_match.end():]
    manifest.write_text(updated_manifest, encoding="utf-8")
    changes.append("manifest_package_removed")
    return {
        "rule": "android_manifest_package_namespace_migration",
        "file": str(manifest.relative_to(project)),
        "gradle_file": str(gradle.relative_to(project)),
        "before": f'package="{namespace}" in source AndroidManifest.xml',
        "after": f'namespace = "{namespace}" in module Gradle; manifest package removed',
        "changes": changes,
        "workspace_only": True,
    }


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


def update_min_sdk(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    matches = re.findall(r"minSdkVersion\s+\d+\s+cannot be smaller than version\s+(\d+)", log, re.I)
    if not matches:
        matches = re.findall(r"uses-sdk:minSdkVersion\s+['\"]?(\d+)['\"]?.*?smaller than version\s+['\"]?(\d+)", log, re.I | re.S)
        required = max((int(pair[1]) for pair in matches), default=0)
    else:
        required = max(map(int, matches))
    if not required:
        return None
    path = app_gradle(project, preflight)
    if not path:
        return None
    text = read_text(path)
    updated, before = update_numeric_property(text, ("minSdk", "minSdkVersion"), required)
    if before is None or updated == text:
        return None
    path.write_text(updated, encoding="utf-8")
    return {"rule": "android_min_sdk_requirement", "file": str(path.relative_to(project)), "before": before, "after": f"minSdk = {required}"}


def update_compile_sdk(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    versions = [int(value) for value in re.findall(r"compile against version\s+(\d+)\s+or later", log, re.I)]
    versions += [int(value) for value in re.findall(r"requires compileSdk(?:Version)?\s*(?:>=|of at least)?\s*(\d+)", log, re.I)]
    if not versions:
        return None
    required = max(versions)
    path = app_gradle(project, preflight)
    if not path:
        return None
    text = read_text(path)
    updated, before = update_numeric_property(text, ("compileSdk", "compileSdkVersion"), required)
    if before is None or updated == text:
        return None
    path.write_text(updated, encoding="utf-8")
    return {"rule": "android_compile_sdk_requirement", "file": str(path.relative_to(project)), "before": before, "after": f"compileSdk = {required}"}


def add_exported(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    if not re.search(r"android:exported needs to be explicitly specified", log, re.I):
        return None
    module = app_module_dir(project, preflight)
    if not module:
        return None
    path = module / "src/main/AndroidManifest.xml"
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


def align_jvm_targets(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    match = re.search(r"Inconsistent JVM-target compatibility.*?\((\d+)\).*?\((\d+)\)", log, re.I | re.S)
    if not match:
        return None
    target = max(int(match.group(1)), int(match.group(2)))
    path = app_gradle(project, preflight)
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



def add_gradle_dependency(path: Path, coordinate: str) -> tuple[bool, str]:
    """Add a dependency to the module Gradle file without touching the repository outside workspace."""
    text = read_text(path)
    if not text:
        return False, "missing_gradle"
    artifact = coordinate.split(":", 2)[:2]
    artifact_key = ":".join(artifact)
    if artifact_key and artifact_key in text:
        # An existing lower version is left intact; adding a second direct declaration lets Gradle
        # resolve the highest compatible version deterministically.
        exact = re.search(rf"{re.escape(artifact_key)}:[^'\"\s)]+", text)
        if exact and exact.group(0) == coordinate:
            return False, "already_present"
    marker = re.search(r"(?m)^(?P<i>\s*)dependencies\s*\{\s*$", text)
    line = f'    implementation("{coordinate}")' if path.suffix == ".kts" else f"    implementation '{coordinate}'"
    if marker:
        indent = marker.group("i") + "    "
        line = f'{indent}implementation("{coordinate}")' if path.suffix == ".kts" else f"{indent}implementation '{coordinate}'"
        updated = text[:marker.end()] + "\n" + line + text[marker.end():]
    else:
        updated = text.rstrip() + "\n\ndependencies {\n" + line + "\n}\n"
    path.write_text(updated, encoding="utf-8")
    return True, "added"


def add_targeted_lint_disable(path: Path, lint_id: str) -> bool:
    text = read_text(path)
    if not text or lint_id in text:
        return False
    android = re.search(r"(?m)^(?P<i>\s*)android\s*\{\s*$", text)
    if not android:
        return False
    indent = android.group("i") + "    "
    if path.suffix == ".kts":
        block = f'\n{indent}lint {{\n{indent}    disable += "{lint_id}"\n{indent}}}\n'
    else:
        block = f"\n{indent}lintOptions {{\n{indent}    disable '{lint_id}'\n{indent}}}\n"
    updated = text[:android.end()] + block + text[android.end():]
    path.write_text(updated, encoding="utf-8")
    return True



def disable_missing_release_signing(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    """Detach a missing source keystore so the isolated publication job can apply fallback signing."""
    patterns = (
        r"signingConfigData\.storeFile specifies file:.*?which doesn[’']t exist",
        r"Keystore file ['\"]?.+?['\"]? not found",
        r"storeFile.*?(?:does not exist|doesn[’']t exist|not found)",
        r"File .*?\.(?:jks|keystore).*?(?:does not exist|doesn[’']t exist|not found)",
    )
    if not any(re.search(pattern, log, re.I | re.S) for pattern in patterns):
        return None
    path = app_gradle(project, preflight)
    if not path:
        return None
    text = read_text(path)
    marker = "Buildino workspace-only missing-keystore override"
    if marker in text:
        return None
    if path.suffix == ".kts":
        override = (
            "\n\n// " + marker + "\n"
            "android {\n"
            "    buildTypes {\n"
            "        getByName(\"release\") {\n"
            "            signingConfig = null\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
    else:
        override = (
            "\n\n// " + marker + "\n"
            "android {\n"
            "    buildTypes {\n"
            "        release {\n"
            "            signingConfig null\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
    path.write_text(text.rstrip() + override, encoding="utf-8")
    missing = None
    match = re.search(r"signingConfigData\.storeFile specifies file:\s*(.+?)\s*(?:which doesn[’']t exist|$)", log, re.I | re.S)
    if match:
        missing = match.group(1).strip().splitlines()[0][:240]
    return {
        "rule": "android_missing_release_keystore",
        "file": str(path.relative_to(project)),
        "before": f"missing Release keystore: {missing or 'configured storeFile'}",
        "after": "release signingConfig detached; isolated fallback signing remains enabled",
        "workspace_only": True,
        "source_modified": False,
    }


def upgrade_ksp_headless_npe(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    """Upgrade only the exact KSP 2.3.5 headless crash when a KSP task itself failed."""
    known_npe = re.search(
        r"ksp\.com\.intellij.*?ApplicationManager\.getApplication\(\).*?is null",
        log,
        re.I | re.S,
    )
    failed_ksp_task = re.search(
        r"(?:Execution failed for task|Task)\s+['\"]?[^\n'\"]*ksp[^\n'\"]*['\"]?.*?(?:FAILED|failed)",
        log,
        re.I | re.S,
    )
    if not known_npe or not failed_ksp_task:
        return None
    skip = {".git", ".gradle", "build", "node_modules", ".dart_tool"}
    candidates: list[Path] = []
    for pattern in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "libs.versions.toml"):
        for path in project.rglob(pattern):
            try:
                rel = path.relative_to(project)
            except ValueError:
                continue
            if any(part in skip for part in rel.parts):
                continue
            candidates.append(path)
    changed_files: list[str] = []
    exact = re.compile(r"(?<![0-9.])2\.3\.5(?![0-9.])")
    for path in sorted(set(candidates))[:120]:
        original = read_text(path)
        updated, count = exact.subn("2.3.6", original)
        if count and updated != original:
            path.write_text(updated, encoding="utf-8")
            changed_files.append(str(path.relative_to(project)))
    if not changed_files:
        return None
    return {
        "rule": "android_ksp_235_headless_npe",
        "file": changed_files[0],
        "files": changed_files,
        "before": "KSP 2.3.5 headless IntelliJ ApplicationManager NPE",
        "after": "KSP 2.3.6",
        "workspace_only": True,
    }

def _android_gradle_properties(project: Path, preflight: dict | None = None) -> Path:
    module = app_module_dir(project, preflight)
    if module:
        try:
            rel = module.relative_to(project)
            if rel.parts and rel.parts[0] == "android":
                return project / "android" / "gradle.properties"
        except ValueError:
            pass
    return project / "gradle.properties"


def _set_gradle_property(path: Path, key: str, value: str) -> tuple[bool, str | None]:
    text = read_text(path)
    pattern = re.compile(rf"(?m)^(?P<prefix>\s*{re.escape(key)}\s*[=:]\s*)(?P<value>[^#\r\n]*)(?P<suffix>\s*(?:#.*)?)$")
    match = pattern.search(text)
    if match:
        current = match.group("value").strip()
        if current.lower() == value.lower():
            return False, current
        replacement = f"{match.group('prefix')}{value}{match.group('suffix')}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text[:match.start()] + replacement + text[match.end():], encoding="utf-8")
        return True, current
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    path.write_text(prefix + f"{key}={value}\n", encoding="utf-8")
    return True, None


def _project_uses_legacy_support(project: Path) -> bool:
    skip = {".git", ".gradle", "build", "node_modules", ".dart_tool"}
    for pattern in ("build.gradle", "build.gradle.kts", "libs.versions.toml"):
        for candidate in project.rglob(pattern):
            try:
                rel = candidate.relative_to(project)
            except ValueError:
                continue
            if any(part in skip for part in rel.parts):
                continue
            if "com.android.support:" in read_text(candidate):
                return True
    return False


def add_appcompat_for_missing_theme(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    theme_missing = bool(re.search(
        r"Theme\.AppCompat(?:\.[A-Za-z0-9_]+)*.*(?:not found|resource.*not found)|resource style/Theme\.AppCompat",
        log,
        re.I | re.S,
    ))
    androidx_disabled = bool(re.search(
        r"AndroidX dependencies.*android\.useAndroidX is not enabled|Set\s+android\.useAndroidX\s*=\s*true|android\.useAndroidX\s+is not enabled",
        log,
        re.I | re.S,
    ))
    if not theme_missing and not androidx_disabled:
        return None
    path = app_gradle(project, preflight)
    if not path:
        return None

    coordinate = "androidx.appcompat:appcompat:1.7.1"
    dependency_added = False
    reason = "not_required"
    if theme_missing:
        dependency_added, reason = add_gradle_dependency(path, coordinate)

    gradle_text = read_text(path)
    uses_androidx = dependency_added or "androidx." in gradle_text or androidx_disabled
    properties_changed: list[str] = []
    properties_path = _android_gradle_properties(project, preflight)
    if uses_androidx:
        changed, _ = _set_gradle_property(properties_path, "android.useAndroidX", "true")
        if changed:
            properties_changed.append("android.useAndroidX=true")
        if _project_uses_legacy_support(project):
            changed, _ = _set_gradle_property(properties_path, "android.enableJetifier", "true")
            if changed:
                properties_changed.append("android.enableJetifier=true")

    if not dependency_added and not properties_changed:
        return None
    return {
        "rule": "android_appcompat_androidx_enablement",
        "file": str(path.relative_to(project)),
        "gradle_properties": str(properties_path.relative_to(project)),
        "before": "Theme.AppCompat unavailable / AndroidX disabled",
        "after": "; ".join(([coordinate] if dependency_added else []) + properties_changed),
        "workspace_only": True,
        "reason": reason,
        "dependency_added": dependency_added,
        "properties_changed": properties_changed,
    }


def upgrade_fragment_for_activity_result(project: Path, log: str, preflight: dict | None = None) -> dict | None:
    if not re.search(r"InvalidFragmentVersionForActivityResult|Upgrade Fragment version to at least\s+1\.3\.0", log, re.I):
        return None
    path = app_gradle(project, preflight)
    if not path:
        return None
    coordinate = "androidx.fragment:fragment:1.3.6"
    dependency_added, _ = add_gradle_dependency(path, coordinate)
    lint_disabled = add_targeted_lint_disable(path, "InvalidFragmentVersionForActivityResult")
    if not dependency_added and not lint_disabled:
        return None
    return {
        "rule": "android_fragment_activity_result_compat",
        "file": str(path.relative_to(project)),
        "before": "Fragment older than 1.3.0 / targeted lint failure",
        "after": f"{coordinate}; disable only InvalidFragmentVersionForActivityResult lint",
        "dependency_added": dependency_added,
        "lint_disabled": lint_disabled,
        "workspace_only": True,
    }

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--build-label", required=True)
    parser.add_argument("--preflight", type=Path)
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    log = args.log.read_text(encoding="utf-8", errors="replace")
    report = load_report(args.output)
    preflight = load_preflight(args.preflight)
    if any(item.get("build_label") == args.build_label for item in report["attempts"]):
        print(json.dumps({"applied_count": 0, "retry_recommended": False, "reason": "already_attempted"}))
        return 0

    attempt = {"build_label": args.build_label, "applied_count": 0, "rules_checked": [], "skipped": []}
    fixers = (disable_missing_release_signing, upgrade_ksp_headless_npe, add_appcompat_for_missing_theme, upgrade_fragment_for_activity_result, migrate_manifest_package, insert_namespace, update_min_sdk, update_compile_sdk, add_exported, align_jvm_targets)
    for fixer in fixers:
        attempt["rules_checked"].append(fixer.__name__)
        try:
            applied = fixer(project, log, preflight)
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
