#!/usr/bin/env bash
set -Eeuo pipefail
: "${SOURCE_ZIP:?SOURCE_ZIP is required}";: "${BUILD_TARGET:?BUILD_TARGET is required}";: "${REQUEST_ID:?REQUEST_ID is required}";: "${BUILDINO_PYTHON_FRAMEWORK:?BUILDINO_PYTHON_FRAMEWORK is required}"
case "$BUILD_TARGET" in apk|aab|both);;*)exit 2;;esac
rm -rf handoff work;mkdir -p handoff/result handoff/logs work/project
status=failure;failure_stage=initialization;failure_kind=infrastructure;failure_code=1;project_dir="";fallback_signing_used=true;java_version="17";flavors_json='[]'
write_status(){ STATUS="$status" FAILURE_STAGE="$failure_stage" FAILURE_KIND="$failure_kind" FAILURE_CODE="$failure_code" PROJECT_DIR="$project_dir" REQUEST_ID="$REQUEST_ID" BUILD_TARGET="$BUILD_TARGET" FALLBACK_SIGNING_USED="$fallback_signing_used" JAVA_VERSION="$java_version" PY_FRAMEWORK="$BUILDINO_PYTHON_FRAMEWORK" python3 - <<'PY'
import hashlib,json,os
from pathlib import Path
outs=[]
for p in sorted(Path('handoff/result').glob('*')):
 if p.is_file() and p.suffix in {'.apk','.aab'}:outs.append({'type':p.suffix[1:],'name':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'size':p.stat().st_size})
data={'status':os.environ['STATUS'],'failure_stage':os.environ['FAILURE_STAGE'],'failure_kind':os.environ['FAILURE_KIND'],'failure_code':int(os.environ['FAILURE_CODE']),'project_dir':os.environ['PROJECT_DIR'],'request_id':os.environ['REQUEST_ID'],'target':os.environ['BUILD_TARGET'],'outputs':outs,'framework':os.environ['PY_FRAMEWORK'],'fallback_signing_used':os.environ.get('FALLBACK_SIGNING_USED')=='true','java_version':os.environ.get('JAVA_VERSION')}
for key,name in [('framework_detection','framework-detection.json'),('project_discovery','project-discovery.json'),('preflight','preflight.json'),('error_report','error-report.json')]:
 p=Path('handoff')/name
 if p.is_file():
  try:data[key]=json.loads(p.read_text())
  except:pass
Path('handoff/status.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
PY
}
create_error(){ local args=(--stage "$failure_stage" --code "$failure_code" --output handoff/error-report.json);[ -s handoff/preflight.json ]&&args+=(--preflight handoff/preflight.json);[ -s handoff/project-discovery.json ]&&args+=(--project-discovery handoff/project-discovery.json);for f in handoff/logs/*.log;do [ -f "$f" ]&&args+=(--log "$f");done;python3 scripts/analyze_build_error.py "${args[@]}"||true;}
on_exit(){ rc=$?;trap - EXIT;if [ "$status" != success ];then [ "$failure_code" -le 1 ]&&failure_code=$rc;[ "$failure_code" -eq 0 ]&&failure_code=1;create_error;fi;write_status;rm -rf work/project work/chaquopy-wrapper;[ "$status" = success ]&&exit 0;exit "$failure_code";};trap on_exit EXIT
transient(){ grep -Eiq 'Temporary failure|timed out|Connection reset|Could not GET|Could not HEAD|HTTP 5[0-9][0-9]|429 Too Many Requests|ReadTimeout|ConnectTimeout' "$1"; }
retry(){ local label="$1";shift;local rc=1;for a in 1 2 3;do local log="handoff/logs/${label}-attempt${a}.log";set +e;(cd "$project_dir"&&"$@")2>&1|tee "$log";rc=${PIPESTATUS[0]};set -e;[ $rc -eq 0 ]&&return 0;if [ $a -lt 3 ]&&transient "$log";then sleep $((a*8));else return $rc;fi;done;return $rc; }
failure_stage=source_validation;failure_kind=user;failure_code=3;python3 scripts/validate_zip.py "$SOURCE_ZIP" 2>&1|tee handoff/logs/source-validation.log
failure_stage=source_extract;failure_code=4;python3 scripts/prepare_source.py "$SOURCE_ZIP" work/project 2>&1|tee handoff/logs/source-extract.log
failure_stage=source_discovery;failure_code=5;project_dir="$(python3 scripts/find_android_project.py work/project --framework "$BUILDINO_PYTHON_FRAMEWORK" --report handoff/project-discovery.json 2>&1|tee handoff/logs/project-discovery.log|tail -n1)";test -d "$project_dir"
[ "${PYTHON_SETUP_OUTCOME:-success}" = success ]||{ failure_stage=python_setup;failure_kind=infrastructure;failure_code=10;exit 10; }
[ "${JAVA8_SETUP_OUTCOME:-success}" = success ]&&[ "${JAVA11_SETUP_OUTCOME:-success}" = success ]&&[ "${JAVA17_SETUP_OUTCOME:-success}" = success ]&&[ "${JAVA21_SETUP_OUTCOME:-success}" = success ]||{ failure_stage=java_setup;failure_kind=infrastructure;failure_code=11;exit 11; }
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
case "$BUILDINO_PYTHON_FRAMEWORK" in
 python_buildozer)
  failure_stage=python_android_toolchain;failure_kind=infrastructure;failure_code=15
  sudo apt-get update -y -qq 2>&1|tee handoff/logs/apt-update.log
  sudo apt-get install -y -qq git zip unzip autoconf automake libtool pkg-config zlib1g-dev libncurses-dev libtinfo6 cmake libffi-dev libssl-dev 2>&1|tee handoff/logs/apt-install.log
  retry python-buildozer-install python -m pip install --upgrade 'pip<26' buildozer cython virtualenv||{ failure_code=$?;exit "$failure_code"; }
  build_one(){ local type="$1";failure_stage="python_buildozer_${type}";failure_kind=user;failure_code=20
    python3 - "$project_dir/buildozer.spec" "$type" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]);kind=sys.argv[2];s=p.read_text(encoding='utf-8',errors='replace')
line=f'android.release_artifact = {kind}'
if re.search(r'(?m)^\s*android\.release_artifact\s*=.*$',s):s=re.sub(r'(?m)^\s*android\.release_artifact\s*=.*$',line,s)
else:s+='\n'+line+'\n'
p.write_text(s,encoding='utf-8')
PY
    retry "buildozer-${type}" buildozer -v android release||{ failure_code=$?;return "$failure_code"; }
    local out;out="$(find "$project_dir/bin" -maxdepth 1 -type f -name "*.${type}" -size +0c|sort|tail -n1)";[ -s "$out" ]||{ failure_stage="${type}_output_missing";failure_code=30;return 30;};cp "$out" "handoff/result/${REQUEST_ID}.${type}"; }
  if [ "$BUILD_TARGET" = apk ]||[ "$BUILD_TARGET" = both ];then build_one apk||exit $?;fi
  if [ "$BUILD_TARGET" = aab ]||[ "$BUILD_TARGET" = both ];then build_one aab||exit $?;fi
 ;;
 python_briefcase)
  failure_stage=python_briefcase_install;failure_kind=user;failure_code=14
  retry briefcase-install python -m pip install --upgrade briefcase||{ failure_code=$?;exit "$failure_code"; }
  retry briefcase-create briefcase create android||{ failure_stage=python_briefcase_create;failure_code=$?;exit "$failure_code"; }
  retry briefcase-build briefcase build android||{ failure_stage=python_briefcase_build;failure_code=$?;exit "$failure_code"; }
  package_one(){ local type="$1";failure_stage="python_briefcase_package_${type}";failure_code=20;retry "briefcase-package-${type}" briefcase package android -p "$type"||{ failure_code=$?;return "$failure_code"; };local out;out="$(find "$project_dir" -type f -name "*.${type}" -size +0c|sort|tail -n1)";[ -s "$out" ]||{ failure_stage="${type}_output_missing";failure_code=30;return 30;};cp "$out" "handoff/result/${REQUEST_ID}.${type}"; }
  if [ "$BUILD_TARGET" = apk ]||[ "$BUILD_TARGET" = both ];then package_one apk||exit $?;fi
  if [ "$BUILD_TARGET" = aab ]||[ "$BUILD_TARGET" = both ];then package_one aab||exit $?;fi
 ;;
 python_chaquopy)
  mkdir -p work/chaquopy-wrapper;cp -a "$project_dir" work/chaquopy-wrapper/android;wrapper="$(realpath work/chaquopy-wrapper)"
  failure_stage=project_preflight;failure_kind=user;failure_code=13;python3 scripts/buildino_preflight.py "$wrapper" handoff/preflight.json 2>&1|tee handoff/logs/preflight.log
  java_version="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["java_version"])')";java_home="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["java_home"])')";fallback_signing_used="$(python3 -c 'import json;print(str(json.load(open("handoff/preflight.json"))["fallback_signing_used"]).lower())')";export JAVA_HOME="$java_home";export PATH="$JAVA_HOME/bin:$PATH";chmod +x "$wrapper/android/gradlew";project_dir="$wrapper"
  gradle_one(){ local type="$1" task out;[ "$type" = apk ]&&task=assembleRelease||task=bundleRelease;failure_stage="python_chaquopy_${type}";failure_code=20;retry "chaquopy-${type}" ./android/gradlew -p android "$task" --no-daemon --stacktrace||{ failure_code=$?;return "$failure_code"; };if [ "$type" = apk ];then out="$(find "$wrapper/android" -type f -iname '*release*.apk' -size +0c|sort|tail -n1)";else out="$(find "$wrapper/android" -type f -iname '*release*.aab' -size +0c|sort|tail -n1)";fi;[ -s "$out" ]||{ failure_stage="${type}_output_missing";failure_code=30;return 30;};cp "$out" "handoff/result/${REQUEST_ID}.${type}"; }
  if [ "$BUILD_TARGET" = apk ]||[ "$BUILD_TARGET" = both ];then gradle_one apk||exit $?;fi
  if [ "$BUILD_TARGET" = aab ]||[ "$BUILD_TARGET" = both ];then gradle_one aab||exit $?;fi
 ;;
esac
status=success;failure_stage=none;failure_kind=none;failure_code=0;write_status
