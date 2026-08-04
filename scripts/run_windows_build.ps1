$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:Framework = 'unknown'

$SourceZip = if ($env:SOURCE_ZIP) { $env:SOURCE_ZIP } else { 'incoming/source.zip' }
$RequestId = if ($env:REQUEST_ID) { $env:REQUEST_ID } else { 'buildino' }
$ResultDir = Join-Path $PWD 'handoff/result'
$LogDir = Join-Path $PWD 'handoff/logs'
$WorkDir = Join-Path $PWD 'work/project'
New-Item -ItemType Directory -Force -Path $ResultDir, $LogDir | Out-Null

function Write-JsonFile([string]$Path, [hashtable]$Value) {
  $Value | ConvertTo-Json -Depth 12 | Set-Content -Path $Path -Encoding UTF8
}

function Fail-Build([string]$Stage, [string]$Category, [int]$Code, [string]$Cause, [string]$Solution) {
  $report = @{
    title = 'خطای ساخت خروجی ویندوز'
    stage = $Stage
    category = $Category
    exit_code = $Code
    cause = $Cause
    solution = $Solution
    framework = $script:Framework
  }
  Write-JsonFile (Join-Path $PWD 'handoff/error-report.json') $report
  Write-JsonFile (Join-Path $PWD 'handoff/status.json') @{
    status = 'failure'
    failure_stage = $Stage
    failure_kind = if ($Category -eq 'infrastructure') { 'infrastructure' } else { 'user' }
    failure_code = $Code
    request_id = $RequestId
    target = 'exe'
    framework = $script:Framework
    outputs = @()
  }
  exit $Code
}

try {
  & python scripts/validate_zip.py $SourceZip 2>&1 | Tee-Object -FilePath (Join-Path $LogDir 'source-validation.log')
  if ($LASTEXITCODE -ne 0) { Fail-Build 'source_validation' 'source' 3 'فایل ZIP معتبر نیست.' 'ساختار ZIP را بررسی کنید.' }
  if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
  & python scripts/prepare_source.py $SourceZip $WorkDir 2>&1 | Tee-Object -FilePath (Join-Path $LogDir 'source-extract.log')
  if ($LASTEXITCODE -ne 0) { Fail-Build 'source_extract' 'source' 4 'استخراج سورس ناموفق بود.' 'فایل ZIP را دوباره ایجاد کنید.' }

  $projectInfo = & python scripts/find_desktop_project.py $WorkDir --target windows --report handoff/project-discovery.json 2>&1
  $projectInfo | Set-Content -Path (Join-Path $LogDir 'project-discovery.log') -Encoding UTF8
  if ($LASTEXITCODE -ne 0) { Fail-Build 'framework_detection' 'project_structure' 5 ($projectInfo -join "`n") 'برای EXE فعلاً پروژه Flutter Windows یا .NET Windows ارسال کنید.' }
  $projectDir = ($projectInfo | Select-Object -Last 1).Trim()
  if (-not (Test-Path $projectDir)) { Fail-Build 'framework_detection' 'project_structure' 5 'مسیر پروژه شناسایی‌شده وجود ندارد.' 'ساختار سورس را بررسی کنید.' }

  $discovery = Get-Content handoff/project-discovery.json -Raw | ConvertFrom-Json
  $script:Framework = [string]$discovery.selected.framework

  if ($script:Framework -eq 'flutter_windows') {
    if (-not (Get-Command flutter -ErrorAction SilentlyContinue)) { Fail-Build 'flutter_setup' 'infrastructure' 43 'Flutter روی Runner ویندوز آماده نشد.' 'Workflow یا سرویس Flutter را دوباره اجرا کنید.' }
    Push-Location $projectDir
    try {
      & flutter config --enable-windows-desktop | Tee-Object -FilePath (Join-Path $LogDir 'flutter-config.log')
      if (-not (Test-Path 'windows')) {
        & flutter create --platforms=windows . 2>&1 | Tee-Object -FilePath (Join-Path $LogDir 'flutter-windows-create.log')
        if ($LASTEXITCODE -ne 0) { Fail-Build 'windows_platform_prepare' 'flutter' 31 'ساخت پوشه Windows ناموفق بود.' 'تنظیمات pubspec و نام پروژه را بررسی کنید.' }
      }
      & flutter pub get 2>&1 | Tee-Object -FilePath (Join-Path $LogDir 'flutter-pub-get.log')
      if ($LASTEXITCODE -ne 0) { Fail-Build 'flutter_dependency_install' 'dependency' 14 'دریافت وابستگی‌های Flutter ناموفق بود.' 'وابستگی‌های pubspec.yaml را بررسی کنید.' }
      & flutter build windows --release 2>&1 | Tee-Object -FilePath (Join-Path $LogDir 'flutter-windows-build.log')
      if ($LASTEXITCODE -ne 0) { Fail-Build 'flutter_windows_build' 'build' 20 'ساخت Flutter Windows ناموفق بود.' 'لاگ Build ویندوز را بررسی کنید.' }
    } finally { Pop-Location }

    $releaseDirs = Get-ChildItem -Path (Join-Path $projectDir 'build/windows') -Directory -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'Release' -and $_.FullName -match '[\\/]runner[\\/]Release$' }
    $releaseDir = $releaseDirs | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $releaseDir) { Fail-Build 'exe_output_missing' 'output' 30 'پوشه خروجی Release پیدا نشد.' 'ساختار خروجی Flutter را بررسی کنید.' }
    $exe = Get-ChildItem $releaseDir.FullName -Filter '*.exe' -File | Where-Object { $_.Name -notmatch '^(crashpad_handler|vc_redist)' } | Sort-Object Length -Descending | Select-Object -First 1
    if (-not $exe) { Fail-Build 'exe_output_missing' 'output' 30 'فایل EXE پیدا نشد.' 'خروجی Release را بررسی کنید.' }
    Copy-Item $exe.FullName (Join-Path $ResultDir "$RequestId.exe") -Force
    Compress-Archive -Path (Join-Path $releaseDir.FullName '*') -DestinationPath (Join-Path $ResultDir "$RequestId-windows-package.zip") -Force
  }
  elseif ($script:Framework -eq 'dotnet_windows') {
    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { Fail-Build 'dotnet_setup' 'infrastructure' 44 '.NET SDK روی Runner آماده نشد.' 'Workflow را دوباره اجرا کنید.' }
    $selectedFile = [string]$discovery.selected.entry_file
    if (-not $selectedFile) { Fail-Build 'dotnet_project_detection' 'project_structure' 6 'فایل csproj یا sln انتخاب نشد.' 'پروژه .NET Windows معتبر ارسال کنید.' }
    $publishDir = Join-Path $PWD 'work/dotnet-publish'
    if (Test-Path $publishDir) { Remove-Item -Recurse -Force $publishDir }
    New-Item -ItemType Directory -Force -Path $publishDir | Out-Null
    Push-Location $projectDir
    try {
      & dotnet restore $selectedFile 2>&1 | Tee-Object -FilePath (Join-Path $LogDir 'dotnet-restore.log')
      if ($LASTEXITCODE -ne 0) { Fail-Build 'dotnet_restore' 'dependency' 14 'بازیابی پکیج‌های .NET ناموفق بود.' 'NuGet و TargetFramework پروژه را بررسی کنید.' }
      & dotnet publish $selectedFile -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -p:PublishReadyToRun=false -o $publishDir 2>&1 | Tee-Object -FilePath (Join-Path $LogDir 'dotnet-publish.log')
      if ($LASTEXITCODE -ne 0) {
        & dotnet publish $selectedFile -c Release -r win-x64 --self-contained false -o $publishDir 2>&1 | Tee-Object -FilePath (Join-Path $LogDir 'dotnet-publish-fallback.log')
        if ($LASTEXITCODE -ne 0) { Fail-Build 'dotnet_windows_build' 'build' 20 'ساخت پروژه .NET Windows ناموفق بود.' 'لاگ dotnet publish را بررسی کنید.' }
      }
    } finally { Pop-Location }
    $exe = Get-ChildItem $publishDir -Filter '*.exe' -File | Sort-Object Length -Descending | Select-Object -First 1
    if (-not $exe) { Fail-Build 'exe_output_missing' 'output' 30 'فایل EXE در خروجی .NET پیدا نشد.' 'OutputType و TargetFramework پروژه را بررسی کنید.' }
    Copy-Item $exe.FullName (Join-Path $ResultDir "$RequestId.exe") -Force
    Compress-Archive -Path (Join-Path $publishDir '*') -DestinationPath (Join-Path $ResultDir "$RequestId-windows-package.zip") -Force
  }
  else {
    Fail-Build 'framework_detection' 'project_structure' 5 "نوع پروژه پشتیبانی نمی‌شود: $script:Framework" 'پروژه Flutter Windows یا .NET Windows ارسال کنید.'
  }

  $outputs = Get-ChildItem $ResultDir -File | ForEach-Object { @{ name=$_.Name; size=$_.Length; type=$_.Extension.TrimStart('.').ToLowerInvariant() } }
  Write-JsonFile (Join-Path $PWD 'handoff/status.json') @{
    status = 'success'
    failure_stage = 'none'
    failure_kind = 'none'
    failure_code = 0
    request_id = $RequestId
    target = 'exe'
    framework = $script:Framework
    outputs = @($outputs)
    package_manager = if ($script:Framework -eq 'dotnet_windows') { 'nuget' } else { 'pub' }
  }
  exit 0
}
catch {
  if (-not (Test-Path 'handoff/status.json')) {
    Fail-Build 'windows_runner_exception' 'infrastructure' 99 $_.Exception.Message 'لاگ Workflow ویندوز را بررسی و دوباره تلاش کنید.'
  }
  exit 99
}
