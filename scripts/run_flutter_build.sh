#!/usr/bin/env bash
set -Eeuo pipefail

: "${SOURCE_ZIP:?SOURCE_ZIP is required}"
: "${BUILD_TARGET:?BUILD_TARGET is required}"
: "${REQUEST_ID:?REQUEST_ID is required}"
case "$BUILD_TARGET" in apk|aab|both) ;; *) echo "Invalid BUILD_TARGET" >&2; exit 2 ;; esac

rm -rf handoff work
mkdir -p handoff/result handoff/logs work/project

status="failure"
failure_stage="initialization"
failure_kind="infrastructure"
failure_code=1
project_dir=""
fallback_signing_used="false"
java_version=""
flavors_json="[]"

write_status() {
  STATUS="$status" FAILURE_STAGE="$failure_stage" FAILURE_KIND="$failure_kind" \
  FAILURE_CODE="$failure_code" PROJECT_DIR="$project_dir" REQUEST_ID="$REQUEST_ID" \
  BUILD_TARGET="$BUILD_TARGET" FALLBACK_SIGNING_USED="$fallback_signing_used" \
  JAVA_VERSION="$java_version" FLAVORS_JSON="$flavors_json" python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
root = Path("handoff")
outputs = []
for path in sorted((root / "result").glob("*")):
    if path.is_file() and path.suffix in {".apk", ".aab"}:
        outputs.append({
            "type": path.suffix[1:], "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        })
data = {
    "status": os.environ["STATUS"],
    "failure_stage": os.environ["FAILURE_STAGE"],
    "failure_kind": os.environ["FAILURE_KIND"],
    "failure_code": int(os.environ["FAILURE_CODE"]),
    "project_dir": os.environ["PROJECT_DIR"],
    "request_id": os.environ["REQUEST_ID"],
    "target": os.environ["BUILD_TARGET"],
    "outputs": outputs,
    "fallback_signing_used": os.environ.get("FALLBACK_SIGNING_USED") == "true",
    "java_version": os.environ.get("JAVA_VERSION") or None,
    "flavors": json.loads(os.environ.get("FLAVORS_JSON", "[]")),
}
for key, filename in (
    ("project_discovery", "project-discovery.json"),
    ("project_prepare", "project-prepare.json"),
    ("preflight", "preflight.json"),
    ("android_components", "android-components.json"),
    ("error_report", "error-report.json"),
):
    path = root / filename
    if path.is_file():
        try:
            data[key] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
applied, attempts = [], []
for filename in ("auto-fixes.json", "adaptive-fixes.json"):
    path = root / filename
    if not path.is_file():
        continue
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        continue
    applied.extend(payload.get("applied", []))
    attempts.extend(payload.get("attempts", []))
data["auto_fixes"] = applied
data["auto_fix_attempts"] = attempts
(root / "status.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

create_error_report() {
  local args=(--stage "$failure_stage" --code "$failure_code" --output handoff/error-report.json)
  [ -s handoff/preflight.json ] && args+=(--preflight handoff/preflight.json)
  [ -s handoff/project-discovery.json ] && args+=(--project-discovery handoff/project-discovery.json)
  [ -s handoff/project-prepare.json ] && args+=(--project-prepare handoff/project-prepare.json)
  [ -s handoff/auto-fixes.json ] && args+=(--auto-fixes handoff/auto-fixes.json)
  [ -s handoff/adaptive-fixes.json ] && args+=(--adaptive-fixes handoff/adaptive-fixes.json)
  for log in handoff/logs/*.log; do [ -f "$log" ] && args+=(--log "$log"); done
  python3 scripts/analyze_build_error.py "${args[@]}" || true
}

on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$status" != "success" ]; then
    if [ "${failure_code:-0}" -le 1 ]; then failure_code="$rc"; fi
    [ "$failure_code" -eq 0 ] && failure_code=1
    create_error_report
  fi
  write_status
  rm -rf work/project
  [ "$status" = "success" ] && exit 0
  exit "${failure_code:-1}"
}
trap on_exit EXIT

is_transient_log() {
  local file="$1"
  grep -Eiq 'UnknownHostException|Connection reset|Read timed out|Could not GET|Could not HEAD|Temporary failure|timed out|HTTP 5[0-9][0-9]|503 Service Unavailable|429 Too Many Requests' "$file"
}

failure_stage="source_validation"; failure_kind="user"; failure_code=3
python3 scripts/validate_zip.py "$SOURCE_ZIP" 2>&1 | tee handoff/logs/source-validation.log

failure_stage="source_extract"; failure_kind="user"; failure_code=4
python3 scripts/prepare_source.py "$SOURCE_ZIP" work/project 2>&1 | tee handoff/logs/source-extract.log

failure_stage="source_discovery"; failure_kind="user"; failure_code=5
project_dir="$(python3 scripts/find_flutter_project.py work/project --report handoff/project-discovery.json 2>&1 | tee handoff/logs/project-discovery.log | tail -n 1)"
test -n "$project_dir" && test -f "$project_dir/pubspec.yaml" && test -d "$project_dir/lib"

if [ "${FLUTTER_SETUP_OUTCOME:-success}" != "success" ]; then
  failure_stage="flutter_setup"; failure_kind="infrastructure"; failure_code=12; exit 12
fi
if [ "${JAVA8_SETUP_OUTCOME:-success}" != "success" ] || [ "${JAVA11_SETUP_OUTCOME:-success}" != "success" ] || [ "${JAVA17_SETUP_OUTCOME:-success}" != "success" ] || [ "${JAVA21_SETUP_OUTCOME:-success}" != "success" ]; then
  failure_stage="java_setup"; failure_kind="infrastructure"; failure_code=11; exit 11
fi

failure_stage="android_platform_prepare"; failure_kind="user"; failure_code=31
python3 scripts/prepare_flutter_platform.py "$project_dir" handoff/project-prepare.json 2>&1 | tee handoff/logs/project-prepare.log

test -d "$project_dir/android" && test -d "$project_dir/android/app"

failure_stage="project_preflight"; failure_kind="user"; failure_code=13
python3 scripts/buildino_preflight.py "$project_dir" handoff/preflight.json 2>&1 | tee handoff/logs/preflight.log
java_version="$(python3 -c 'import json; print(json.load(open("handoff/preflight.json"))["java_version"])')"
java_home="$(python3 -c 'import json; print(json.load(open("handoff/preflight.json"))["java_home"])')"
fallback_signing_used="$(python3 -c 'import json; print(str(json.load(open("handoff/preflight.json"))["fallback_signing_used"]).lower())')"
flavors_json="$(python3 -c 'import json; print(json.dumps(json.load(open("handoff/preflight.json"))["flavors"], separators=(",",":")))')"
export JAVA_HOME="$java_home"
export PATH="$JAVA_HOME/bin:$PATH"
java -version 2>&1 | tee handoff/logs/java-selected.log

failure_stage="android_sdk_prepare"; failure_kind="infrastructure"; failure_code=15
python3 scripts/ensure_android_components.py handoff/preflight.json handoff/android-components.json 2>&1 | tee handoff/logs/android-components.log || true

failure_stage="flutter_pub_get"; failure_kind="user"; failure_code=14
pub_rc=1
for attempt in 1 2 3; do
  log="handoff/logs/flutter-pub-get-attempt${attempt}.log"
  set +e
  (cd "$project_dir" && flutter --version && flutter pub get) 2>&1 | tee "$log"
  pub_rc=${PIPESTATUS[0]}
  set -e
  [ "$pub_rc" -eq 0 ] && break
  if [ "$attempt" -lt 3 ] && is_transient_log "$log"; then
    sleep $((attempt * 8))
    continue
  fi
  break
done
[ "$pub_rc" -eq 0 ] || { failure_code="$pub_rc"; exit "$pub_rc"; }

mapfile -t flavors < <(python3 -c 'import json; print("\n".join(json.load(open("handoff/preflight.json"))["flavors"]))')
if [ "${#flavors[@]}" -eq 0 ]; then flavors=(""); fi

build_one() {
  local type="$1" flavor="$2" suffix output_dir pattern output_path label
  local -a cmd
  if [ "$type" = "apk" ]; then
    suffix="apk"; output_dir="$project_dir/build/app/outputs/flutter-apk"; pattern='*release*.apk'
    cmd=(flutter build apk --release)
  else
    suffix="aab"; output_dir="$project_dir/build/app/outputs/bundle"; pattern='*release*.aab'
    cmd=(flutter build appbundle --release)
  fi
  [ -n "$flavor" ] && cmd+=(--flavor "$flavor")
  local entrypoint=""
  entrypoint="$(python3 - "$project_dir" "$flavor" <<'PY'
from pathlib import Path
import json, sys
project = Path(sys.argv[1])
flavor = sys.argv[2]
report = json.load(open("handoff/preflight.json", encoding="utf-8"))
entrypoints = [value for value in report.get("entrypoints", []) if isinstance(value, str)]
preferred = []
if flavor:
    normalized = flavor.lower().replace("-", "_")
    preferred.extend([f"lib/main_{normalized}.dart", f"lib/main-{normalized}.dart"])
preferred.append("lib/main.dart")
for candidate in preferred:
    if candidate in entrypoints and (project / candidate).is_file():
        print(candidate)
        raise SystemExit(0)
if len(entrypoints) == 1 and (project / entrypoints[0]).is_file():
    print(entrypoints[0])
PY
)"
  [ -n "$entrypoint" ] && cmd+=(--target "$entrypoint")
  label="${type}${flavor:+-$flavor}"

  local log rc=1 attempt=1
  log="handoff/logs/flutter-build-${label}-attempt1.log"
  failure_stage="flutter_build_${label//-/_}"; failure_kind="user"; failure_code=20
  set +e
  (cd "$project_dir" && "${cmd[@]}") 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  set -e

  # One network-only retry does not modify source.
  if [ "$rc" -ne 0 ] && is_transient_log "$log"; then
    attempt=2
    failure_stage="flutter_build_${label//-/_}_network_retry"
    sleep 10
    log="handoff/logs/flutter-build-${label}-attempt2-network.log"
    set +e
    (cd "$project_dir" && "${cmd[@]}") 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    set -e
  fi

  if [ "$rc" -ne 0 ]; then
    failure_stage="flutter_autofix_${label//-/_}"; failure_kind="user"; failure_code="$rc"
    local theme_summary adaptive_summary theme_count adaptive_count total_fixes
    theme_summary="$(python3 scripts/apply_flutter_compat_fixes.py \
      --project "$project_dir" --log "$log" --output handoff/auto-fixes.json \
      --build-label "$label" 2>&1 || true)"
    adaptive_summary="$(python3 scripts/apply_adaptive_project_fixes.py \
      --project "$project_dir" --log "$log" --output handoff/adaptive-fixes.json \
      --build-label "$label" 2>&1 || true)"
    printf '%s\n%s\n' "$theme_summary" "$adaptive_summary" | tee "handoff/logs/flutter-autofix-${label}.log"
    theme_count="$(python3 -c 'import json,sys; print(int(json.loads(sys.argv[1]).get("applied_count",0)))' "$theme_summary" 2>/dev/null || echo 0)"
    adaptive_count="$(python3 -c 'import json,sys; print(int(json.loads(sys.argv[1]).get("applied_count",0)))' "$adaptive_summary" 2>/dev/null || echo 0)"
    total_fixes=$((theme_count + adaptive_count))
    if [ "$total_fixes" -gt 0 ]; then
      attempt=$((attempt + 1))
      failure_stage="flutter_build_${label//-/_}_after_adaptive_fix"; failure_code=20
      log="handoff/logs/flutter-build-${label}-attempt${attempt}-autofix.log"
      set +e
      (cd "$project_dir" && "${cmd[@]}") 2>&1 | tee "$log"
      rc=${PIPESTATUS[0]}
      set -e
    fi
  fi

  if [ "$rc" -ne 0 ]; then
    failure_stage="flutter_build_${label//-/_}_final"; failure_code="$rc"
    return "$rc"
  fi

  if [ "$type" = "apk" ]; then
    output_path="$(find "$output_dir" -maxdepth 1 -type f -name "$pattern" -size +0c | { [ -n "$flavor" ] && grep -i "$flavor" || cat; } | sort | tail -n 1 || true)"
  else
    output_path="$(find "$output_dir" -type f -name "$pattern" -size +0c | { [ -n "$flavor" ] && grep -i "$flavor" || cat; } | sort | tail -n 1 || true)"
  fi
  if [ -z "$output_path" ] || [ ! -s "$output_path" ]; then
    failure_stage="${type}_output_missing"; failure_code=30; return 30
  fi
  cp "$output_path" "handoff/result/${REQUEST_ID}${flavor:+-$flavor}.${suffix}"
}

if [ "$BUILD_TARGET" = "apk" ] || [ "$BUILD_TARGET" = "both" ]; then
  for flavor in "${flavors[@]}"; do build_one apk "$flavor" || exit $?; done
fi
if [ "$BUILD_TARGET" = "aab" ] || [ "$BUILD_TARGET" = "both" ]; then
  for flavor in "${flavors[@]}"; do build_one aab "$flavor" || exit $?; done
fi

status="success"; failure_stage="none"; failure_kind="none"; failure_code=0
write_status
