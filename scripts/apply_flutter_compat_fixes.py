#!/usr/bin/env python3
"""Apply narrowly-scoped Flutter compatibility migrations after a compiler failure.

The fixer intentionally supports only deterministic framework API migrations that can
be proven from the compiler diagnostic. It never rewrites business logic and never
runs more than once for the same build label.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    rule_id: str
    old_type: str
    new_type: str
    property_name: str
    official_migration: str


RULES: tuple[Rule, ...] = (
    Rule(
        "flutter_component_theme_card_data",
        "CardTheme",
        "CardThemeData",
        "cardTheme",
        "https://docs.flutter.dev/release/breaking-changes/component-theme-normalization",
    ),
    Rule(
        "flutter_component_theme_dialog_data",
        "DialogTheme",
        "DialogThemeData",
        "dialogTheme",
        "https://docs.flutter.dev/release/breaking-changes/component-theme-normalization",
    ),
    Rule(
        "flutter_component_theme_tabbar_data",
        "TabBarTheme",
        "TabBarThemeData",
        "tabBarTheme",
        "https://docs.flutter.dev/release/breaking-changes/component-theme-normalization",
    ),
)

ERROR_RE = re.compile(
    r"(?m)^(?P<file>(?:[A-Za-z]:)?[^\n:]+\.dart):(?P<line>\d+):(?P<column>\d+):\s+Error:\s+"
    r"The argument type '(?P<old>[A-Za-z0-9_]+)' can't be assigned to the parameter type "
    r"'(?P<new>[A-Za-z0-9_]+)\?'\."
)


def safe_project_file(project: Path, diagnostic_path: str) -> Path | None:
    raw = Path(diagnostic_path.strip())
    candidate = raw if raw.is_absolute() else project / raw
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project)
    except (OSError, ValueError):
        return None
    if resolved.suffix != ".dart" or not resolved.is_file():
        return None
    return resolved


def replace_constructor_near_line(text: str, line_number: int, rule: Rule) -> tuple[str, int] | None:
    lines = text.splitlines(keepends=True)
    if not lines:
        return None
    target = max(0, min(line_number - 1, len(lines) - 1))
    # Compiler locations can point at the argument expression or its property line.
    start = max(0, target - 3)
    end = min(len(lines), target + 5)
    constructor = re.compile(rf"\b{re.escape(rule.old_type)}\s*\(")
    property_marker = re.compile(rf"\b{re.escape(rule.property_name)}\s*:")

    ranked: list[tuple[int, int]] = []
    for index in range(start, end):
        if constructor.search(lines[index]):
            distance = abs(index - target)
            context = "".join(lines[max(0, index - 2): index + 1])
            property_penalty = 0 if property_marker.search(context) else 100
            ranked.append((property_penalty + distance, index))
    if not ranked:
        return None
    _, index = min(ranked)
    changed, count = constructor.subn(f"{rule.new_type}(", lines[index], count=1)
    if count != 1:
        return None
    lines[index] = changed
    return "".join(lines), index + 1


def load_existing(path: Path) -> dict:
    if not path.is_file():
        return {"schema": 1, "applied": [], "attempts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": 1, "applied": [], "attempts": []}
    if not isinstance(data, dict):
        return {"schema": 1, "applied": [], "attempts": []}
    data.setdefault("schema", 1)
    data.setdefault("applied", [])
    data.setdefault("attempts", [])
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--build-label", required=True)
    args = parser.parse_args()

    project = args.project.resolve(strict=True)
    log_text = args.log.read_text(encoding="utf-8", errors="replace")
    report = load_existing(args.output)

    # Enforce a single automatic migration attempt per concrete build label.
    if any(item.get("build_label") == args.build_label for item in report["attempts"]):
        print(json.dumps({"applied_count": 0, "retry_recommended": False, "reason": "already_attempted"}))
        return 0

    attempt: dict = {
        "build_label": args.build_label,
        "diagnostics_found": 0,
        "applied_count": 0,
        "skipped": [],
    }
    pending: list[tuple[Rule, Path, int, int]] = []
    seen: set[tuple[str, int, str]] = set()

    by_pair = {(rule.old_type, rule.new_type): rule for rule in RULES}
    for match in ERROR_RE.finditer(log_text):
        rule = by_pair.get((match.group("old"), match.group("new")))
        if not rule:
            continue
        attempt["diagnostics_found"] += 1
        file_path = safe_project_file(project, match.group("file"))
        if file_path is None:
            attempt["skipped"].append({"reason": "unsafe_or_missing_path", "file": match.group("file")})
            continue
        line = int(match.group("line"))
        key = (str(file_path), line, rule.rule_id)
        if key in seen:
            continue
        seen.add(key)
        pending.append((rule, file_path, line, int(match.group("column"))))

    # Group by file so multiple migrations in the same Dart file are committed atomically.
    grouped: dict[Path, list[tuple[Rule, int, int]]] = {}
    for rule, file_path, line, column in pending:
        grouped.setdefault(file_path, []).append((rule, line, column))

    staged: list[dict] = []
    for file_path, fixes in grouped.items():
        original = file_path.read_text(encoding="utf-8", errors="strict")
        updated = original
        file_changes: list[dict] = []
        for rule, line, column in sorted(fixes, key=lambda item: item[1]):
            result = replace_constructor_near_line(updated, line, rule)
            if result is None:
                attempt["skipped"].append({
                    "reason": "constructor_not_found_near_diagnostic",
                    "file": str(file_path.relative_to(project)),
                    "line": line,
                    "rule": rule.rule_id,
                })
                continue
            updated, actual_line = result
            file_changes.append({
                "rule": rule.rule_id,
                "file": str(file_path.relative_to(project)),
                "diagnostic_line": line,
                "actual_line": actual_line,
                "column": column,
                "before": f"{rule.old_type}(",
                "after": f"{rule.new_type}(",
                "property": rule.property_name,
                "official_migration": rule.official_migration,
                "build_label": args.build_label,
            })
        if file_changes:
            staged.append({"path": file_path, "text": updated, "changes": file_changes})

    # Write only after all files were read and transformed successfully.
    for item in staged:
        item["path"].write_text(item["text"], encoding="utf-8")
        report["applied"].extend(item["changes"])
        attempt["applied_count"] += len(item["changes"])

    report["attempts"].append(attempt)
    report["retry_recommended"] = attempt["applied_count"] > 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {
        "applied_count": attempt["applied_count"],
        "retry_recommended": attempt["applied_count"] > 0,
        "build_label": args.build_label,
        "rules": [item["rule"] for item in report["applied"] if item.get("build_label") == args.build_label],
    }
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
