#!/usr/bin/env bash
set -Eeuo pipefail
: "${SOURCE_ZIP:?SOURCE_ZIP is required}"
: "${BUILD_TARGET:?BUILD_TARGET is required}"
: "${REQUEST_ID:?REQUEST_ID is required}"
case "$BUILD_TARGET" in apk|aab|both) ;; *) echo "Invalid BUILD_TARGET" >&2; exit 2;; esac

rm -rf framework-probe handoff
mkdir -p framework-probe handoff/result handoff/logs
failure_stage="framework_detection"
failure_kind="user"
failure_code=5
framework="unknown"

write_dispatch_failure() {
  local rc="$1"
  [ "$rc" -eq 0 ] && rc=1
  local args=(--stage "$failure_stage" --code "$rc" --output handoff/error-report.json)
  for log in handoff/logs/*.log; do [ -f "$log" ] && args+=(--log "$log"); done
  python3 scripts/analyze_build_error.py "${args[@]}" || true
  FAILURE_STAGE="$failure_stage" FAILURE_KIND="$failure_kind" FAILURE_CODE="$rc" FRAMEWORK="$framework" python3 - <<'PY'
import json,os
from pathlib import Path
report={}
if Path('handoff/error-report.json').is_file():
    report=json.loads(Path('handoff/error-report.json').read_text(encoding='utf-8'))
Path('handoff/status.json').write_text(json.dumps({
    'status':'failure','failure_stage':os.environ['FAILURE_STAGE'],
    'failure_kind':os.environ['FAILURE_KIND'],'failure_code':int(os.environ['FAILURE_CODE']),
    'request_id':os.environ.get('REQUEST_ID'),'target':os.environ.get('BUILD_TARGET'),
    'outputs':[],'framework':os.environ.get('FRAMEWORK','unknown'),'error_report':report,
},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
PY
}

on_exit() {
  local rc=$?
  trap - EXIT
  rm -rf framework-probe
  if [ "$rc" -ne 0 ] && [ ! -s handoff/status.json ]; then
    write_dispatch_failure "$rc"
  fi
  exit "$rc"
}
trap on_exit EXIT

failure_stage="source_validation"; failure_code=3
python3 scripts/validate_zip.py "$SOURCE_ZIP" 2>&1 | tee handoff/logs/framework-source-validation.log
failure_stage="source_extract"; failure_code=4
python3 scripts/prepare_source.py "$SOURCE_ZIP" framework-probe 2>&1 | tee handoff/logs/framework-source-extract.log
failure_stage="framework_detection"; failure_code=5
project_dir="$(python3 scripts/find_android_project.py framework-probe --report handoff/framework-detection.json 2>&1 | tee handoff/logs/framework-detection.log | tail -n 1)"
test -n "$project_dir"
framework="$(python3 -c 'import json; print(json.load(open("handoff/framework-detection.json"))["selected"]["framework"])')"
rm -rf framework-probe

set +e
case "$framework" in
  flutter) bash scripts/run_flutter_build.sh ; rc=$? ;;
  react_native) bash scripts/run_react_native_build.sh ; rc=$? ;;
  native_android) bash scripts/run_native_android_build.sh ; rc=$? ;;
  python_buildozer|python_briefcase|python_chaquopy)
    BUILDINO_PYTHON_FRAMEWORK="$framework" bash scripts/run_python_android_build.sh ; rc=$? ;;
  *) echo "Unsupported framework: $framework" >&2; rc=5 ;;
esac
set -e

if [ -s handoff/status.json ]; then
  FRAMEWORK="$framework" python3 - <<'PY'
import json,os
from pathlib import Path
p=Path('handoff/status.json')
data=json.loads(p.read_text(encoding='utf-8'))
data['framework']=os.environ['FRAMEWORK']
p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
PY
fi
exit "$rc"
