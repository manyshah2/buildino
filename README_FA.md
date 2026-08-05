# Buildino Public Runner Template v0.11.6

این قالب برای Build چندسکویی بیلدینو استفاده می‌شود.

## خروجی‌های پشتیبانی‌شده

- Android: APK / AAB / هر دو
- iOS: Flutter IPA روی `macos-latest`
- Windows: Flutter یا .NET به‌صورت EXE و بسته کامل ZIP روی `windows-latest`

## فایل‌های فعال

```text
.github/workflows/buildino-runner-wf17.yml
.github/workflows/buildino-cleanup-wf14.yml
scripts/run_android_build.sh
scripts/run_windows_build.ps1
scripts/run_ios_build.sh
scripts/find_desktop_project.py
```

IPA بدون Secrets اپل به‌صورت unsigned ساخته می‌شود. برای امضای اختیاری، Secrets P12، Provisioning Profile و ExportOptions در Repository Runner تنظیم می‌شوند.

## Java Android

AGP 8 با Java 17 اجرا می‌شود و خطای Runtime اعلام‌شده توسط Gradle یک‌بار به‌صورت خودکار با Java مناسب تکرار می‌شود.

## سازگاری Gradle محلی

اگر `org.gradle.java.home` به مسیر مخصوص دستگاه کاربر اشاره کند و روی Runner وجود نداشته باشد، فقط در Workspace موقت غیرفعال می‌شود. سورس اصلی و Repository کاربر تغییر نمی‌کند. پروژه‌های بدون Wrapper با Gradle ایزوله و سازگار اجرا می‌شوند.
