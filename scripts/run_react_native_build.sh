#!/usr/bin/env bash
set -Eeuo pipefail
: "${SOURCE_ZIP:?SOURCE_ZIP is required}"
: "${BUILD_TARGET:?BUILD_TARGET is required}"
: "${REQUEST_ID:?REQUEST_ID is required}"
case "$BUILD_TARGET" in apk|aab|both) ;; *) exit 2;; esac
rm -rf handoff work
mkdir -p handoff/result handoff/logs work/project
status=failure;failure_stage=initialization;failure_kind=infrastructure;failure_code=1
project_dir="";fallback_signing_used=false;java_version="";flavors_json='[]';package_manager=""
write_status(){ STATUS="$status" FAILURE_STAGE="$failure_stage" FAILURE_KIND="$failure_kind" FAILURE_CODE="$failure_code" PROJECT_DIR="$project_dir" REQUEST_ID="$REQUEST_ID" BUILD_TARGET="$BUILD_TARGET" FALLBACK_SIGNING_USED="$fallback_signing_used" JAVA_VERSION="$java_version" FLAVORS_JSON="$flavors_json" PACKAGE_MANAGER="$package_manager" python3 - <<'PY'
import hashlib,json,os
from pathlib import Path
outputs=[]
for p in sorted(Path('handoff/result').glob('*')):
 if p.is_file() and p.suffix in {'.apk','.aab'}: outputs.append({'type':p.suffix[1:],'name':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'size':p.stat().st_size})
data={'status':os.environ['STATUS'],'failure_stage':os.environ['FAILURE_STAGE'],'failure_kind':os.environ['FAILURE_KIND'],'failure_code':int(os.environ['FAILURE_CODE']),'project_dir':os.environ['PROJECT_DIR'],'request_id':os.environ['REQUEST_ID'],'target':os.environ['BUILD_TARGET'],'outputs':outputs,'framework':'react_native','package_manager':os.environ.get('PACKAGE_MANAGER'),'fallback_signing_used':os.environ.get('FALLBACK_SIGNING_USED')=='true','java_version':os.environ.get('JAVA_VERSION') or None,'flavors':json.loads(os.environ.get('FLAVORS_JSON','[]'))}
for key,name in [('framework_detection','framework-detection.json'),('project_discovery','project-discovery.json'),('preflight','preflight.json'),('gradle_java_home_fixes','gradle-java-home-fixes.json'),('error_report','error-report.json')]:
 p=Path('handoff')/name
 if p.is_file():
  try:data[key]=json.loads(p.read_text())
  except:pass
Path('handoff/status.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
PY
}
create_error(){ local args=(--stage "$failure_stage" --code "$failure_code" --output handoff/error-report.json); [ -s handoff/preflight.json ]&&args+=(--preflight handoff/preflight.json); [ -s handoff/project-discovery.json ]&&args+=(--project-discovery handoff/project-discovery.json); for f in handoff/logs/*.log;do [ -f "$f" ]&&args+=(--log "$f");done; python3 scripts/analyze_build_error.py "${args[@]}"||true; }
on_exit(){ rc=$?;trap - EXIT;if [ "$status" != success ];then [ "$failure_code" -le 1 ]&&failure_code=$rc;[ "$failure_code" -eq 0 ]&&failure_code=1;create_error;fi;write_status;rm -rf work/project;[ "$status" = success ]&&exit 0;exit "$failure_code";};trap on_exit EXIT
transient(){ grep -Eiq 'ENOTFOUND|ECONNRESET|ETIMEDOUT|EAI_AGAIN|UnknownHostException|Connection reset|Read timed out|Could not GET|Could not HEAD|HTTP 5[0-9][0-9]|429 Too Many Requests' "$1"; }
java_required_by_log(){ python3 scripts/android_java_runtime.py required-from-log "$1" 2>/dev/null||true; }
activate_java_runtime(){ local required="$1" reason="$2" var home;var="JAVA_HOME_${required}_X64";home="${!var:-}";[ -n "$home" ]||return 1;python3 scripts/android_java_runtime.py apply handoff/preflight.json "$required" --reason "$reason" >"handoff/logs/java-runtime-switch-${required}.json";java_version="$required";java_home="$home";export JAVA_HOME="$java_home";export PATH="$JAVA_HOME/bin:$PATH";java -version 2>&1|tee -a handoff/logs/java-selected.log; }
retry_cmd(){ local label="$1";shift;local rc=1 required_java;for a in 1 2 3;do local log="handoff/logs/${label}-attempt${a}.log";set +e;(cd "$project_dir"&&"$@") 2>&1|tee "$log";rc=${PIPESTATUS[0]};set -e;[ $rc -eq 0 ]&&return 0;required_java="$(java_required_by_log "$log")";if [ $a -lt 3 ]&&[ -n "$required_java" ]&&[ "${java_version:-0}" -lt "$required_java" ]&&activate_java_runtime "$required_java" "React Native Gradle requested Java ${required_java}";then sleep 2;continue;fi;if [ $a -lt 3 ]&&transient "$log";then sleep $((a*8));else return $rc;fi;done;return $rc; }
failure_stage=source_validation;failure_kind=user;failure_code=3;python3 scripts/validate_zip.py "$SOURCE_ZIP" 2>&1|tee handoff/logs/source-validation.log
failure_stage=source_extract;failure_code=4;python3 scripts/prepare_source.py "$SOURCE_ZIP" work/project 2>&1|tee handoff/logs/source-extract.log
failure_stage=source_discovery;failure_code=5;project_dir="$(python3 scripts/find_android_project.py work/project --framework react_native --report handoff/project-discovery.json 2>&1|tee handoff/logs/project-discovery.log|tail -n1)";test -f "$project_dir/package.json"
[ "${NODE_SETUP_OUTCOME:-success}" = success ]||{ failure_stage=node_setup;failure_kind=infrastructure;failure_code=10;exit 10; }
[ "${JAVA8_SETUP_OUTCOME:-success}" = success ]&&[ "${JAVA11_SETUP_OUTCOME:-success}" = success ]&&[ "${JAVA17_SETUP_OUTCOME:-success}" = success ]&&[ "${JAVA21_SETUP_OUTCOME:-success}" = success ]||{ failure_stage=java_setup;failure_kind=infrastructure;failure_code=11;exit 11; }
if [ -f "$project_dir/pnpm-lock.yaml" ];then package_manager=pnpm;corepack enable;install=(pnpm install --frozen-lockfile)
elif [ -f "$project_dir/yarn.lock" ];then package_manager=yarn;corepack enable;install=(yarn install --immutable)
elif [ -f "$project_dir/package-lock.json" ];then package_manager=npm;install=(npm ci --no-audit --no-fund)
else package_manager=npm;install=(npm install --no-audit --no-fund);fi
failure_stage=javascript_dependency_install;failure_kind=user;failure_code=14;retry_cmd rn-dependency-install "${install[@]}"||{ failure_code=$?;exit "$failure_code"; }
if [ ! -d "$project_dir/android" ];then
 failure_stage=react_native_android_prepare;failure_code=31
 if python3 - "$project_dir/package.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]));d={**p.get('dependencies',{}),**p.get('devDependencies',{})};raise SystemExit(0 if 'expo' in d else 1)
PY
 then retry_cmd expo-prebuild npx expo prebuild --platform android --no-install||{ failure_code=$?;exit "$failure_code"; }
 else echo 'React Native android directory is missing and this is not a compatible Expo project.' >&2;exit 31;fi
fi
test -d "$project_dir/android/app"
failure_stage=gradle_java_home_sanitize;failure_kind=infrastructure;failure_code=17;python3 scripts/sanitize_gradle_java_home.py "$project_dir" handoff/gradle-java-home-fixes.json 2>&1|tee handoff/logs/gradle-java-home-fixes.log
failure_stage=project_preflight;failure_kind=user;failure_code=13;python3 scripts/buildino_preflight.py "$project_dir" handoff/preflight.json 2>&1|tee handoff/logs/preflight.log
java_version="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["java_version"])')";java_home="$(python3 -c 'import json;print(json.load(open("handoff/preflight.json"))["java_home"])')";fallback_signing_used="$(python3 -c 'import json;print(str(json.load(open("handoff/preflight.json"))["fallback_signing_used"]).lower())')";flavors_json="$(python3 -c 'import json;print(json.dumps(json.load(open("handoff/preflight.json"))["flavors"],separators=(",",":")))')";export JAVA_HOME="$java_home";export PATH="$JAVA_HOME/bin:$PATH"
chmod +x "$project_dir/android/gradlew"
mapfile -t flavors < <(python3 -c 'import json;print("\n".join(json.load(open("handoff/preflight.json"))["flavors"]))');[ ${#flavors[@]} -gt 0 ]||flavors=("")
build_one(){ local type="$1" flavor="$2" task cap out;cap="${flavor^}";if [ "$type" = apk ];then task="assemble${cap}Release";else task="bundle${cap}Release";fi;failure_stage="react_native_gradle_${type}${flavor:+_}${flavor}";failure_code=20;retry_cmd "rn-${task}" ./android/gradlew -p android "$task" --no-daemon --stacktrace||{ failure_code=$?;return "$failure_code"; };if [ "$type" = apk ];then out="$(find "$project_dir/android/app/build/outputs/apk" -type f -iname '*release*.apk' -size +0c|{ [ -n "$flavor" ]&&grep -i "$flavor"||cat; }|sort|tail -n1)";else out="$(find "$project_dir/android/app/build/outputs/bundle" -type f -iname '*release*.aab' -size +0c|{ [ -n "$flavor" ]&&grep -i "$flavor"||cat; }|sort|tail -n1)";fi;[ -s "$out" ]||{ failure_stage="${type}_output_missing";failure_code=30;return 30;};cp "$out" "handoff/result/${REQUEST_ID}${flavor:+-$flavor}.${type}"; }
if [ "$BUILD_TARGET" = apk ]||[ "$BUILD_TARGET" = both ];then for f in "${flavors[@]}";do build_one apk "$f"||exit $?;done;fi
if [ "$BUILD_TARGET" = aab ]||[ "$BUILD_TARGET" = both ];then for f in "${flavors[@]}";do build_one aab "$f"||exit $?;done;fi
status=success;failure_stage=none;failure_kind=none;failure_code=0;write_status
