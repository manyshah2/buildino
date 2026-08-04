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
module_dir=""
module_path=""
java_version=""
gradle_version=""
agp_version=""
language=""
flavors_json="[]"
fallback_signing_used="true"
gradle_mode=""
declare -a GRADLE_CMD=()

write_status() {
  STATUS="$status" FAILURE_STAGE="$failure_stage" FAILURE_KIND="$failure_kind" \
  FAILURE_CODE="$failure_code" PROJECT_DIR="$project_dir" MODULE_DIR="$module_dir" \
  MODULE_PATH="$module_path" REQUEST_ID="$REQUEST_ID" BUILD_TARGET="$BUILD_TARGET" \
  JAVA_VERSION="$java_version" GRADLE_VERSION="$gradle_version" AGP_VERSION="$agp_version" \
  LANGUAGE="$language" FLAVORS_JSON="$flavors_json" GRADLE_MODE="$gradle_mode" \
  FALLBACK_SIGNING_USED="$fallback_signing_used" python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
outputs=[]
for path in sorted(Path("handoff/result").glob("*")):
    if path.is_file() and path.suffix in {".apk", ".aab"}:
        outputs.append({
            "type": path.suffix[1:], "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        })
data={
    "status": os.environ["STATUS"],
    "failure_stage": os.environ["FAILURE_STAGE"],
    "failure_kind": os.environ["FAILURE_KIND"],
    "failure_code": int(os.environ["FAILURE_CODE"]),
    "project_dir": os.environ["PROJECT_DIR"],
    "module_dir": os.environ["MODULE_DIR"],
    "module_path": os.environ["MODULE_PATH"],
    "request_id": os.environ["REQUEST_ID"],
    "target": os.environ["BUILD_TARGET"],
    "outputs": outputs,
    "framework": "native_android",
    "native_language": os.environ.get("LANGUAGE") or None,
    "java_version": os.environ.get("JAVA_VERSION") or None,
    "gradle_version": os.environ.get("GRADLE_VERSION") or None,
    "agp_version": os.environ.get("AGP_VERSION") or None,
    "gradle_mode": os.environ.get("GRADLE_MODE") or None,
    "flavors": json.loads(os.environ.get("FLAVORS_JSON", "[]")),
    "fallback_signing_used": os.environ.get("FALLBACK_SIGNING_USED") == "true",
}
for key, filename in (
    ("framework_detection", "framework-detection.json"),
    ("project_discovery", "project-discovery.json"),
    ("preflight", "preflight.json"),
    ("android_components", "android-components.json"),
    ("error_report", "error-report.json"),
):
    path=Path("handoff") / filename
    if path.is_file():
        try: data[key]=json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError: pass
Path("handoff/status.json").write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
PY
}

create_error_report() {
  local args=(--stage "$failure_stage" --code "$failure_code" --output handoff/error-report.json)
  [ -s handoff/preflight.json ] && args+=(--preflight handoff/preflight.json)
  [ -s handoff/project-discovery.json ] && args+=(--project-discovery handoff/project-discovery.json)
  for log in handoff/logs/*.log; do [ -f "$log" ] && args+=(--log "$log"); done
  python3 scripts/analyze_build_error.py "${args[@]}" || true
}

on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$status" != "success" ]; then
    [ "$failure_code" -le 1 ] && failure_code="$rc"
    [ "$failure_code" -eq 0 ] && failure_code=1
    create_error_report
  fi
  write_status
  rm -rf work/project /tmp/buildino-gradle-*
  [ "$status" = "success" ] && exit 0
  exit "$failure_code"
}
trap on_exit EXIT

is_transient_log() {
  grep -Eiq 'UnknownHostException|Connection reset|Read timed out|Could not GET|Could not HEAD|Temporary failure|timed out|HTTP 5[0-9][0-9]|503 Service Unavailable|429 Too Many Requests' "$1"
}

run_gradle_retry() {
  local label="$1"; shift
  local rc=1
  for attempt in 1 2 3; do
    local log="handoff/logs/${label}-attempt${attempt}.log"
    set +e
    (cd "$project_dir" && "${GRADLE_CMD[@]}" "$@" --no-daemon --stacktrace --warning-mode all) 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    set -e
    [ "$rc" -eq 0 ] && return 0
    if [ "$attempt" -lt 3 ] && is_transient_log "$log"; then
      sleep $((attempt * 10))
    else
      return "$rc"
    fi
  done
  return "$rc"
}

install_fallback_gradle() {
  local version="$1" destination="/tmp/buildino-gradle-${version}"
  local archive="/tmp/buildino-gradle-${version}.zip"
  if [ ! -x "$destination/gradle-${version}/bin/gradle" ]; then
    rm -rf "$destination" "$archive"
    mkdir -p "$destination"
    curl --fail-with-body --location --retry 4 --retry-all-errors \
      --connect-timeout 20 --max-time 900 \
      "https://services.gradle.org/distributions/gradle-${version}-bin.zip" \
      -o "$archive"
    unzip -q "$archive" -d "$destination"
  fi
  GRADLE_CMD=("$destination/gradle-${version}/bin/gradle")
  gradle_mode="downloaded_fallback"
}

failure_stage="source_validation"; failure_kind="user"; failure_code=3
python3 scripts/validate_zip.py "$SOURCE_ZIP" 2>&1 | tee handoff/logs/source-validation.log
failure_stage="source_extract"; failure_code=4
python3 scripts/prepare_source.py "$SOURCE_ZIP" work/project 2>&1 | tee handoff/logs/source-extract.log
failure_stage="source_discovery"; failure_code=5
project_dir="$(python3 scripts/find_android_project.py work/project --framework native_android --report handoff/project-discovery.json 2>&1 | tee handoff/logs/project-discovery.log | tail -n 1)"
test -n "$project_dir"

if [ "${JAVA8_SETUP_OUTCOME:-success}" != "success" ] || \
   [ "${JAVA11_SETUP_OUTCOME:-success}" != "success" ] || \
   [ "${JAVA17_SETUP_OUTCOME:-success}" != "success" ] || \
   [ "${JAVA21_SETUP_OUTCOME:-success}" != "success" ]; then
  failure_stage="java_setup"; failure_kind="infrastructure"; failure_code=11; exit 11
fi

failure_stage="native_android_preflight"; failure_kind="user"; failure_code=13
python3 scripts/native_android_preflight.py "$project_dir" handoff/preflight.json 2>&1 | tee handoff/logs/preflight.log
java_version="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["java_version"])')"
java_home="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["java_home"])')"
module_dir="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["module_dir"])')"
module_path="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["module_path"])')"
gradle_version="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["gradle_version"])')"
agp_version="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json")).get("agp_version") or "")')"
language="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["language"])')"
flavors_json="$(python3 -c 'import json;print(json.dumps(json.load(open("handoff/preflight.json"))["flavors"],separators=(",",":")))')"
export JAVA_HOME="$java_home"
export PATH="$JAVA_HOME/bin:$PATH"
java -version 2>&1 | tee handoff/logs/java-selected.log

failure_stage="android_sdk_prepare"; failure_kind="infrastructure"; failure_code=15
python3 scripts/ensure_android_components.py handoff/preflight.json handoff/android-components.json 2>&1 | tee handoff/logs/android-components.log || true

if [ -f "$project_dir/gradlew" ]; then
  sed -i 's/\r$//' "$project_dir/gradlew"
  chmod +x "$project_dir/gradlew"
  GRADLE_CMD=("./gradlew")
  gradle_mode="wrapper"
else
  failure_stage="gradle_fallback_setup"; failure_kind="infrastructure"; failure_code=16
  install_fallback_gradle "$gradle_version" 2>&1 | tee handoff/logs/gradle-fallback-setup.log
fi

prefix="$module_path"
if [ -n "$prefix" ]; then prefix="${prefix}:"; fi

copy_outputs() {
  local type="$1" base count=0
  if [ "$type" = "apk" ]; then
    base="$module_dir/build/outputs/apk"
    while IFS= read -r path; do
      [ -s "$path" ] || continue
      count=$((count+1))
      stem="$(basename "$path" .apk | tr -cs 'A-Za-z0-9._-' '-')"
      cp "$path" "handoff/result/${REQUEST_ID}-${count}-${stem}.apk"
    done < <(find "$base" -type f -iname '*.apk' -size +0c ! -iname '*androidTest*' 2>/dev/null | sort)
  else
    base="$module_dir/build/outputs/bundle"
    while IFS= read -r path; do
      [ -s "$path" ] || continue
      count=$((count+1))
      stem="$(basename "$path" .aab | tr -cs 'A-Za-z0-9._-' '-')"
      cp "$path" "handoff/result/${REQUEST_ID}-${count}-${stem}.aab"
    done < <(find "$base" -type f -iname '*.aab' -size +0c 2>/dev/null | sort)
  fi
  [ "$count" -gt 0 ] || return 30
}

build_one() {
  local type="$1" task rc
  if [ "$type" = "apk" ]; then task="${prefix}assembleRelease"; else task="${prefix}bundleRelease"; fi
  failure_stage="native_android_${type}_build"; failure_kind="user"; failure_code=20
  run_gradle_retry "native-${type}" "$task" || { rc=$?; failure_code="$rc"; return "$rc"; }
  failure_stage="${type}_output_collect"; failure_code=30
  copy_outputs "$type" || return 30
}

if [ "$BUILD_TARGET" = "apk" ] || [ "$BUILD_TARGET" = "both" ]; then build_one apk || exit $?; fi
if [ "$BUILD_TARGET" = "aab" ] || [ "$BUILD_TARGET" = "both" ]; then build_one aab || exit $?; fi

status="success"; failure_stage="none"; failure_kind="none"; failure_code=0
write_status
