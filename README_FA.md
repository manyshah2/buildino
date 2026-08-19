# Buildino Public Runner Template v0.13.0

این قالب برای Build چندسکویی بیلدینو استفاده می‌شود.

## فایل‌های فعال

```text
.github/workflows/buildino-runner-wf21.yml
.github/workflows/buildino-cleanup-wf18.yml
scripts/run_android_build.sh
scripts/run_native_android_build.sh
scripts/run_flutter_build.sh
```

## بازیابی Android

- Auto-Fixهای مرحله‌ای برای خطاهای زنجیره‌ای اجرا می‌شوند.
- `org.gradle.java.home` نامعتبر فقط در Workspace موقت غیرفعال می‌شود.
- خطای parsing گزینه‌های JVM در `gradlew` با اجرای مستقیم همان نسخه Gradle در Workspace موقت بازیابی می‌شود.
- Keystore مفقود سورس باعث توقف خروجی آزمایشی نمی‌شود؛ SigningConfig موقتاً جدا و امضای fallback در Job ایزوله اعمال می‌شود.
- علت، نتیجه و راه‌حل خطا از اولین Failure واقعی Gradle استخراج می‌شود.

## Callback نسخه 0.13.0

Runner فیلدهای ساختاریافته خطا را برای محلی‌سازی پیام نهایی کاربر به Worker ارسال می‌کند. Log فنی Runner تغییر زبان نمی‌دهد.
