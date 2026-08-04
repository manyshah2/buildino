#!/usr/bin/env bash
set -Eeuo pipefail
SOURCE_ZIP="${SOURCE_ZIP:-incoming/source.zip}"
REQUEST_ID="${REQUEST_ID:?REQUEST_ID is required}"
mkdir -p handoff/result handoff/logs handoff/ios_archive work
status=failure; framework=flutter_ios; failure_stage=runner_execution; failure_kind=user; failure_code=1; ios_signing=unsigned

write_status(){
  STATUS="$status" FRAMEWORK="$framework" FAILURE_STAGE="$failure_stage" FAILURE_KIND="$failure_kind" FAILURE_CODE="$failure_code" IOS_SIGNING="$ios_signing" python3 - <<'PY'
import json,os
from pathlib import Path
result=Path('handoff/result')
outputs=[]
if result.is_dir():
    for p in sorted(result.iterdir()):
        if p.is_file() and p.suffix.lower() in {'.ipa','.zip'}:
            outputs.append({'type':p.suffix.lower().lstrip('.'),'name':p.name,'size':p.stat().st_size})
Path('handoff/status.json').write_text(json.dumps({
 'status':os.environ['STATUS'],'failure_stage':os.environ['FAILURE_STAGE'],'failure_kind':os.environ['FAILURE_KIND'],
 'failure_code':int(os.environ['FAILURE_CODE']),'request_id':os.environ['REQUEST_ID'],'target':'ipa',
 'framework':os.environ['FRAMEWORK'],'ios_signing':os.environ['IOS_SIGNING'],'outputs':outputs,'package_manager':'pub'
},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
PY
}
write_error(){
  CAUSE="$1" SOLUTION="$2" STAGE="$failure_stage" CODE="$failure_code" python3 - <<'PY'
import json,os
from pathlib import Path
Path('handoff/error-report.json').write_text(json.dumps({
 'title':'خطای ساخت IPA','stage':os.environ['STAGE'],'category':'ios_build','exit_code':int(os.environ['CODE']),
 'cause':os.environ['CAUSE'],'solution':os.environ['SOLUTION'],'framework':'flutter_ios'
},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
PY
}
on_exit(){ rc=$?; trap - EXIT; if [ "$status" != success ]; then [ "$failure_code" -le 1 ] && failure_code=$rc; [ "$failure_code" -eq 0 ] && failure_code=1; write_error "ساخت IPA ناموفق شد؛ جزئیات در لاگ Workflow ثبت شده است." "لاگ مرحله ${failure_stage} را بررسی کنید."; fi; write_status; rm -rf work/project; exit "$([ "$status" = success ] && echo 0 || echo "$failure_code")"; }
trap on_exit EXIT

failure_stage=source_validation;failure_code=3
python3 scripts/validate_zip.py "$SOURCE_ZIP" 2>&1 | tee handoff/logs/source-validation.log
failure_stage=source_extract;failure_code=4
rm -rf work/project
python3 scripts/prepare_source.py "$SOURCE_ZIP" work/project 2>&1 | tee handoff/logs/source-extract.log
failure_stage=framework_detection;failure_code=5
project_dir="$(python3 scripts/find_desktop_project.py work/project --target ios --report handoff/project-discovery.json 2>&1 | tee handoff/logs/project-discovery.log | tail -n1)"
test -f "$project_dir/pubspec.yaml"
command -v flutter >/dev/null || { failure_stage=flutter_setup;failure_kind=infrastructure;failure_code=43;exit 43; }

root_dir="$PWD"
pushd "$project_dir" >/dev/null
failure_stage=flutter_dependency_install;failure_code=14
flutter pub get 2>&1 | tee "$root_dir/handoff/logs/flutter-pub-get.log"
if [ ! -d ios ]; then
  failure_stage=ios_platform_prepare;failure_code=31
  flutter create --platforms=ios . 2>&1 | tee "$root_dir/handoff/logs/flutter-ios-create.log"
fi

failure_stage=flutter_ios_unsigned_build;failure_code=20
flutter build ipa --release --no-codesign 2>&1 | tee "$root_dir/handoff/logs/flutter-ios-build.log"
archive="$(find build/ios/archive -maxdepth 1 -type d -name '*.xcarchive' 2>/dev/null | sort | head -n1)"
app="$(find build/ios/archive build/ios/iphoneos -type d -name '*.app' 2>/dev/null | sort | head -n1)"
test -d "$app" || { failure_stage=ipa_output_missing;failure_code=30;exit 30; }
if [ -d "$archive" ]; then
  rm -rf "$root_dir/handoff/ios_archive"/*
  ditto "$archive" "$root_dir/handoff/ios_archive/$(basename "$archive")"
fi
rm -rf /tmp/buildino-payload && mkdir -p /tmp/buildino-payload/Payload
ditto "$app" "/tmp/buildino-payload/Payload/$(basename "$app")"
(cd /tmp/buildino-payload && zip -qry "$root_dir/handoff/result/${REQUEST_ID}-unsigned.ipa" Payload)
popd >/dev/null
status=success;failure_stage=none;failure_kind=none;failure_code=0
write_status
