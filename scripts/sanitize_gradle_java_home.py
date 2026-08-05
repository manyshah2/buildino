#!/usr/bin/env python3
"""Disable machine-specific org.gradle.java.home values in the temporary Buildino workspace."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

SKIP_PARTS = {".git", ".gradle", "build", "node_modules", ".dart_tool"}
PROPERTY_RE = re.compile(r"^(?P<indent>\s*)org\.gradle\.java\.home\s*(?P<sep>=|:)\s*(?P<value>.*?)\s*$")


def decoded_value(raw: str) -> str:
    value = raw.strip().strip('"').strip("'")
    value = value.replace(r"\\ ", " ").replace(r"\\:", ":")
    value = os.path.expandvars(os.path.expanduser(value))
    return value


def valid_java_home(raw: str) -> bool:
    value = decoded_value(raw)
    if not value:
        return False
    home = Path(value)
    java = home / "bin" / ("java.exe" if os.name == "nt" else "java")
    return home.is_dir() and java.is_file()


def candidate_files(project: Path) -> list[Path]:
    files: list[Path] = []
    for path in project.rglob("gradle.properties"):
        try:
            rel = path.relative_to(project)
        except ValueError:
            continue
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        files.append(path)
    return sorted(set(files))[:100]


def sanitize_file(path: Path, project: Path) -> tuple[bool, list[dict[str, object]], int]:
    try:
        original = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, [], 0
    changed = False
    disabled: list[dict[str, object]] = []
    valid_count = 0
    output: list[str] = []
    for number, line in enumerate(original.splitlines(keepends=True), 1):
        logical = line.rstrip("\r\n")
        ending = line[len(logical):]
        match = PROPERTY_RE.match(logical)
        if not match:
            output.append(line)
            continue
        raw_value = match.group("value")
        if valid_java_home(raw_value):
            valid_count += 1
            output.append(line)
            continue
        changed = True
        rel = str(path.relative_to(project))
        disabled.append({
            "file": rel,
            "line": number,
            "original_value": raw_value,
            "resolved_value": decoded_value(raw_value),
            "reason": "Path does not exist or has no Java executable on this Runner",
        })
        output.append(f"{match.group('indent')}# Buildino disabled invalid org.gradle.java.home from source workspace{ending}")
        output.append(f"{match.group('indent')}# org.gradle.java.home={raw_value}{ending}")
    if changed:
        path.write_text("".join(output), encoding="utf-8")
    return changed, disabled, valid_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    report = {
        "schema": 1,
        "project_dir": str(project),
        "workspace_only": True,
        "changed_files": [],
        "disabled": [],
        "valid_values_kept": 0,
    }
    for path in candidate_files(project):
        changed, disabled, valid_count = sanitize_file(path, project)
        report["valid_values_kept"] += valid_count
        if changed:
            report["changed_files"].append(str(path.relative_to(project)))
            report["disabled"].extend(disabled)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["disabled"]:
        print(f"Buildino disabled {len(report['disabled'])} invalid org.gradle.java.home value(s) in the temporary workspace.")
    else:
        print("No invalid org.gradle.java.home value was found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
