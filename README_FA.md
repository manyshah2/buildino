# Buildino Public Runner Template v0.11.2

این قالب برای Build چندسکویی بیلدینو استفاده می‌شود.

## خروجی‌های پشتیبانی‌شده

- Android: APK / AAB / هر دو
- iOS: Flutter IPA روی `macos-latest`
- Windows: Flutter یا .NET به‌صورت EXE و بسته کامل ZIP روی `windows-latest`

## فایل‌های فعال

```text
.github/workflows/buildino-runner-wf16.yml
.github/workflows/buildino-cleanup-wf13.yml
scripts/run_android_build.sh
scripts/run_windows_build.ps1
scripts/run_ios_build.sh
scripts/find_desktop_project.py
```

IPA بدون Secrets اپل به‌صورت unsigned ساخته می‌شود. برای امضای اختیاری، Secrets P12، Provisioning Profile و ExportOptions در Repository Runner تنظیم می‌شوند.

## Java Android

AGP 8 با Java 17 اجرا می‌شود و خطای Runtime اعلام‌شده توسط Gradle یک‌بار به‌صورت خودکار با Java مناسب تکرار می‌شود.
