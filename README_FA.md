# Buildino Public Runner Template v0.11.8

این قالب برای Build چندسکویی بیلدینو استفاده می‌شود.

## خروجی‌های پشتیبانی‌شده

- Android: APK / AAB / هر دو
- iOS: Flutter IPA روی `macos-latest`
- Windows: Flutter یا .NET به‌صورت EXE و بسته کامل ZIP روی `windows-latest`

## فایل‌های فعال

```text
.github/workflows/buildino-runner-wf19.yml
.github/workflows/buildino-cleanup-wf16.yml
scripts/run_android_build.sh
scripts/run_windows_build.ps1
scripts/run_ios_build.sh
```

## سازگاری Android

- Java متناسب با AGP و Gradle انتخاب می‌شود.
- اگر AGP در لاگ حداقل Gradle جدیدتری اعلام کند، همان نسخه به‌صورت ایزوله دریافت و Build تکرار می‌شود.
- `org.gradle.java.home` نامعتبر فقط در Workspace موقت غیرفعال می‌شود.
- `package` قدیمی Manifest فقط در Workspace موقت به `namespace` ماژول منتقل می‌شود.
- سورس اصلی و Repository کاربر هیچ‌وقت Commit یا بازنویسی نمی‌شوند.
