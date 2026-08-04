#!/usr/bin/env python3
"""Best-effort installation of Android SDK components requested by preflight."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def existing(component: str, sdk: Path) -> bool:
    family, _, version = component.partition(";")
    mapping = {
        "platforms": sdk / "platforms" / f"android-{version}",
        "build-tools": sdk / "build-tools" / version,
        "ndk": sdk / "ndk" / version,
        "cmake": sdk / "cmake" / version,
    }
    path = mapping.get(family)
    return bool(path and path.exists())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("preflight", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.preflight.read_text(encoding="utf-8"))
    sdk = Path(os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or "/usr/local/lib/android/sdk")
    requested: list[str] = []
    if data.get("compile_sdk"):
        requested.append(f"platforms;android-{data['compile_sdk']}")
    if data.get("build_tools_version"):
        requested.append(f"build-tools;{data['build_tools_version']}")
    if data.get("ndk_version") and data.get("ndk_version") != "flutter.ndkVersion":
        requested.append(f"ndk;{data['ndk_version']}")
    if data.get("cmake_version"):
        requested.append(f"cmake;{data['cmake_version']}")
    missing = [item for item in requested if not existing(item, sdk)]
    report = {"schema": 1, "sdk_root": str(sdk), "requested": requested, "missing": missing, "installed": [], "warnings": []}
    sdkmanager = shutil.which("sdkmanager")
    if missing and sdkmanager:
        command = [sdkmanager, *missing]
        completed = subprocess.run(command, input="y\n" * 20, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        report["sdkmanager_exit_code"] = completed.returncode
        report["sdkmanager_output_tail"] = completed.stdout[-3000:]
        report["installed"] = [item for item in missing if existing(item, sdk)]
        unresolved = [item for item in missing if item not in report["installed"]]
        if unresolved:
            report["warnings"].append("SDK components could not be preinstalled: " + ", ".join(unresolved))
    elif missing:
        report["warnings"].append("sdkmanager is unavailable; Gradle will attempt component resolution")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    # Best-effort by design. Build is allowed to continue and produce the authoritative error.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
