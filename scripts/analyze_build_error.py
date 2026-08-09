#!/usr/bin/env python3
"""Create a sanitized, user-facing Persian build failure report."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

RULES = [
    ("source_structure", [r"No Flutter or Dart application project candidate was found", r"No supported Android project was found", r"Selected project does not contain pubspec\.yaml", r"Selected project does not contain a lib directory"],
     "ساختار سورس یا ریشه پروژه", "بیلدینو نتوانست یک پروژه Android پشتیبانی‌شده را از ساختار ZIP انتخاب کند.",
     "برای Flutter وجود pubspec.yaml و lib، و برای Java/Kotlin وجود settings.gradle و ماژول دارای com.android.application را بررسی کنید."),
    ("android_platform_prepare", [r"Android platform remains incomplete", r"Flutter could not generate a complete Android platform", r"android_platform_prepare"],
     "آماده‌سازی پلتفرم Android", "پوشه Android وجود نداشت یا ناقص بود و تولید/ادغام خودکار آن کامل نشد.",
     "جزئیات Project Prepare را بررسی کنید؛ مسیر Overlay و خروجی هر روش flutter create در گزارش ثبت شده است."),
    ("android_missing_keystore", [r"signingConfigData\.storeFile specifies file:.*?which doesn[’']t exist", r"Keystore file .*? not found", r"storeFile.*?(?:does not exist|doesn[’']t exist|not found)"],
     "فایل Keystore نسخه Release پیدا نشد", "پروژه برای Build نسخه Release به یک فایل JKS/Keystore اشاره می‌کند که داخل سورس موجود نیست.",
     "بیلدینو باید فقط در Workspace موقت signingConfig نامعتبر را جدا کند، خروجی بدون امضای سورس را بسازد و در مرحله انتشار با Keystore fallback خودش امضا کند."),
    ("android_signing", [r"Missing android/key\.properties", r"Keystore was tampered", r"Failed to read key", r"SigningConfig .* missing required property", r"keyAlias.*(?:missing|invalid)"],
     "خطای امضای Android", "اطلاعات یا فایل امضای Release پروژه ناقص یا نامعتبر است.",
     "برای خروجی آزمایشی، بیلدینو از امضای fallback استفاده می‌کند. برای انتشار یا آپدیت اپ، Keystore اصلی همان برنامه لازم است."),
    ("android_appcompat", [r"Theme\.AppCompat(?:\.[A-Za-z0-9_]+)*.*?(?:not found|resource.*not found)", r"resource style/Theme\.AppCompat", r"AndroidX dependencies.*android\.useAndroidX is not enabled", r"Set\s+android\.useAndroidX\s*=\s*true"],
     "وابستگی AppCompat یا فعال‌سازی AndroidX ناقص است", "پروژه از AppCompat/AndroidX استفاده می‌کند اما Dependency یا تنظیم android.useAndroidX کامل نیست.",
     "بیلدینو AppCompat و android.useAndroidX=true را فقط در Workspace موقت اعمال می‌کند و در پروژه‌های دارای Support Library قدیمی، Jetifier را نیز موقتاً فعال می‌کند."),
    ("android_fragment_activity_result", [r"InvalidFragmentVersionForActivityResult", r"Upgrade Fragment version to at least\s+1\.3\.0"],
     "نسخه Fragment برای Activity Result قدیمی است", "پروژه از Activity Result API استفاده می‌کند اما نسخه Fragment آن قدیمی‌تر از حداقل سازگار است.",
     "بیلدینو نسخه Fragment را فقط در Workspace موقت ارتقا می‌دهد و در صورت نیاز فقط همان Lint ID را غیرفعال می‌کند."),
    ("ksp_headless", [r"Execution failed for task.*ksp", r"Task .*ksp.*FAILED"],
     "خطای KSP در محیط Headless", "Task مربوط به KSP در محیط خط فرمان متوقف شده است و ممکن است نسخه KSP با Kotlin یا اجرای Headless سازگار نباشد.",
     "نسخه KSP را با Kotlin هماهنگ کنید؛ بیلدینو فقط برای خطای شناخته‌شده KSP 2.3.5 آن را در Workspace موقت به 2.3.6 ارتقا می‌دهد."),
    ("gradle_portability", [r"org\.gradle\.java\.home.*invalid", r"Java home supplied is invalid", r"version: unbound variable", r"gradle_fallback_setup"],
     "تنظیمات محلی Gradle", "پروژه یک مسیر Java مخصوص دستگاه سازنده داشت یا آماده‌سازی Gradle جایگزین کامل نشد.",
     "بیلدینو مسیر نامعتبر را فقط در Workspace موقت غیرفعال و Gradle سازگار را روی Runner آماده می‌کند."),
    ("gradle_minimum_version", [r"Minimum supported Gradle version is", r"requires Gradle\s*[0-9].*or newer", r"Gradle version\s*[0-9].*or higher is required"],
     "نسخه Gradle پایین‌تر از نیاز پروژه", "Android Gradle Plugin پروژه به نسخه جدیدتری از Gradle نیاز دارد.",
     "بیلدینو نسخه حداقل اعلام‌شده در لاگ را فقط روی Runner دریافت و Build را بدون تغییر سورس دوباره اجرا می‌کند."),
    ("manifest_package_namespace", [r"Incorrect package=.*found in source AndroidManifest\.xml", r"Setting the namespace via the package attribute.*no longer supported"],
     "انتقال Package قدیمی Manifest به Namespace", "پروژه قدیمی نام Package را داخل AndroidManifest قرار داده و AGP جدید آن را نمی‌پذیرد.",
     "بیلدینو در Workspace موقت Package را به namespace ماژول منتقل و Build را یک‌بار دوباره اجرا می‌کند."),
    ("native_android_build", [r"native_android_", r"Task .*assembleRelease.* not found", r"No Android application module", r"com\.android\.application"],
     "خطای پروژه Native Android", "پروژه Java/Kotlin شناسایی شد اما ماژول برنامه یا Task ساخت Release کامل نبود.",
     "فایل‌های settings.gradle، build.gradle و Plugin com.android.application را بررسی کنید؛ Task مورد انتظار assembleRelease یا bundleRelease است."),
    ("java_gradle", [r"Unsupported class file major version", r"invalid source release", r"requires Java", r"JVM target compatibility"],
     "ناسازگاری Java و Gradle", "نسخه Java موردنیاز پروژه با Gradle، AGP یا Kotlin هماهنگ نیست.",
     "نسخه Java، Gradle Wrapper، Android Gradle Plugin و Kotlin را با هم هماهنگ کنید."),
    ("dependency_network", [r"Could not resolve", r"Could not GET", r"Could not HEAD", r"UnknownHostException", r"Read timed out", r"Connection reset"],
     "دریافت Dependency یا شبکه", "Gradle یا Pub نتوانسته یکی از وابستگی‌ها را از مخزن دریافت کند.",
     "آدرس Repository و نسخه Dependency را بررسی کنید؛ خطاهای موقت شبکه را دوباره اجرا کنید."),
    ("pub_get", [r"version solving failed", r"Because .* depends on", r"pub get failed", r"doesn't match any versions"],
     "تعارض Dependencyهای Flutter", "حل نسخه‌های pubspec ناموفق است یا یک Package با SDK سازگار نیست.",
     "محدوده نسخه Packageها، sdk constraint و dependency_overrides را بررسی کنید."),
    ("javascript_dependency", [r"npm ERR!", r"YN[0-9]{4}", r"ERR_PNPM", r"Could not resolve dependency", r"ERESOLVE"],
     "خطای Dependencyهای JavaScript", "نصب Dependencyهای React Native با npm، Yarn یا pnpm ناموفق شد.",
     "اولین خطای Package Manager را بررسی کنید؛ نسخه Node.js، Lockfile و Packageهای ناسازگار را هماهنگ کنید."),
    ("react_native_compile", [r"Unable to resolve module", r"Metro.*error", r"SyntaxError", r"TypeError:.*undefined", r"React Native.*build failed"],
     "خطای کدنویسی React Native", "باندل JavaScript/TypeScript یا کد Native پروژه React Native کامپایل نشد.",
     "اولین خطای Metro، TypeScript یا Gradle را اصلاح کنید؛ خطاهای بعدی معمولاً پیامد همان خطای اول هستند."),
    ("python_android_dependency", [r"No matching distribution found", r"Could not find a version that satisfies", r"ModuleNotFoundError", r"ResolutionImpossible"],
     "خطای Dependencyهای Python Android", "نصب یا حل Dependencyهای Python برای خروجی Android ناموفق شد.",
     "نسخه Python و Packageهای ثبت‌شده در buildozer.spec یا pyproject.toml را بررسی کنید."),
    ("python_android_build", [r"buildozer.*error", r"python-for-android", r"briefcase.*error", r"Chaquopy", r"Command failed:.*android"],
     "خطای ساخت Python Android", "ابزار Android پروژه Python نتوانست APK/AAB تولید کند.",
     "اولین خطای Buildozer، Briefcase، python-for-android یا Chaquopy را بررسی کنید."),
    ("dart_compile", [r"Error: .*\.dart:", r"Target kernel_snapshot_program failed", r"Compilation failed", r"The getter .* isn't defined"],
     "خطای کدنویسی Dart/Flutter", "کامپایل سورس Dart به‌دلیل خطای نحوی، نوع داده یا API نامعتبر متوقف شده است.",
     "اولین خطای Dart را اصلاح کنید؛ خطاهای بعدی معمولاً پیامد همان خطای اول هستند."),
    ("manifest", [r"Manifest merger failed", r"uses-sdk:minSdkVersion", r"android:exported"],
     "خطای AndroidManifest", "Manifest اصلی یا Manifest یکی از Pluginها با تنظیمات پروژه تعارض دارد.",
     "گزارش Manifest merger را بررسی و minSdk، exported، permission یا placeholder متعارض را اصلاح کنید."),
    ("sdk_ndk", [r"NDK.*not found", r"failed to find target with hash", r"compileSdk", r"platforms;android", r"CMake"],
     "Android SDK/NDK", "نسخه SDK، Build Tools، NDK یا CMake موردنیاز پروژه نصب یا سازگار نیست.",
     "نسخه‌های compileSdk، ndkVersion و CMake را با محیط Build هماهنگ کنید."),
    ("kotlin", [r"Kotlin compilation error", r"e: file://", r"Compilation error\. See log", r"Inconsistent JVM-target"],
     "خطای Kotlin", "کامپایل کد Kotlin یا Plugin اندرویدی ناموفق شده است.",
     "اولین پیام e: را بررسی و نسخه Kotlin/JVM target یا کد Plugin را اصلاح کنید."),
    ("resource", [r"Android resource linking failed", r"resource .* not found", r"AAPT2"],
     "خطای Resource اندروید", "یک Resource، Theme، Attribute یا فایل XML نامعتبر یا مفقود است.",
     "اولین فایل و شماره خط گزارش‌شده توسط AAPT2 را اصلاح کنید."),
    ("disk_memory", [r"No space left on device", r"Java heap space", r"OutOfMemoryError", r"Killed"],
     "کمبود منابع Runner", "فضای دیسک یا حافظه Runner برای این Build کافی نبوده است.",
     "Cacheها و خروجی‌های اضافی را حذف یا مصرف حافظه Gradle را کاهش دهید؛ این خطا سهمیه کاربر را مصرف نمی‌کند."),
]

RESULT_BY_CATEGORY = {
    "source_structure": "ایراد اصلی از ساختار سورس است و بیلدینو پروژه قابل‌ساختی پیدا نکرده است.",
    "android_platform_prepare": "مشکل از ناقص‌بودن بخش Android سورس است؛ بیلدینو تولید یا ادغام موقت را امتحان کرده اما کامل نشده است.",
    "android_missing_keystore": "ایراد اصلی از تنظیمات امضای سورس است؛ بیلدینو باید آن را فقط در Workspace موقت دور بزند و خروجی را با امضای fallback آماده کند.",
    "android_signing": "ایراد اصلی از اطلاعات امضای سورس است؛ در صورت امکان بیلدینو فقط برای خروجی آزمایشی از امضای fallback استفاده می‌کند.",
    "android_appcompat": "ایراد اصلی از Dependency ناقص سورس است؛ این مورد باید توسط Auto-Fix موقت بیلدینو نیز قابل بازیابی باشد.",
    "android_fragment_activity_result": "ایراد اصلی از نسخه قدیمی Dependency سورس است؛ بیلدینو باید آن را موقتاً ارتقا دهد.",
    "ksp_headless": "ایراد از سازگاری KSP پروژه با محیط خط فرمان است؛ بیلدینو فقط الگوی شناخته‌شده و دقیق را موقتاً اصلاح می‌کند.",
    "gradle_portability": "مشکل از تنظیمات محلی سورس یا آماده‌سازی Gradle روی Runner است؛ تغییر فقط در Workspace موقت انجام می‌شود.",
    "gradle_minimum_version": "نسخه Gradle انتخاب‌شده پایین‌تر از نیاز AGP پروژه بوده و بیلدینو باید Runtime سازگار را جایگزین کند.",
    "manifest_package_namespace": "ایراد از ساختار قدیمی سورس است و بیلدینو باید مهاجرت موقت Package به Namespace را انجام دهد.",
    "dependency_network": "این خطا می‌تواند از شبکه Runner یا Repository وابستگی باشد و برای تشخیص نهایی باید اولین خطای Resolve بررسی شود.",
    "resource": "ایراد اصلی از Resource یا Dependency سورس است؛ اگر الگوی شناخته‌شده باشد Auto-Fix موقت اجرا می‌شود.",
    "disk_memory": "این خطا از منابع Runner است و نباید به‌عنوان ایراد سورس یا مصرف موفق سهمیه ثبت شود.",
    "unknown": "علت قطعی هنوز از لاگ استخراج نشده و نیاز به بررسی اولین خطای واقعی Gradle دارد.",
}

SECRET_PATTERNS = [
    re.compile(r"(?i)(storePassword|keyPassword|password|token|secret|api[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"gh[oprsu]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
]


def sanitize(line: str) -> str:
    result = line.strip().replace("\x1b", "")
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(lambda m: m.group(0).split(m.group(1), 1)[0] + m.group(1) + "=<redacted>" if m.lastindex else "<redacted>", result)
    result = re.sub(r"/home/runner/work/[^/]+/[^/]+/work/project/", "<project>/", result)
    return result[:600]


def select_excerpt(lines: list[str]) -> list[str]:
    high_priority = ("* What went wrong:", "Execution failed for task", "FAILURE: Build failed", "BUILD FAILED")
    high_indices = [i for i, line in enumerate(lines) if any(marker.lower() in line.lower() for marker in high_priority)]
    if high_indices:
        start = high_indices[0]
        for index in high_indices:
            if "* what went wrong:" in lines[index].lower():
                start = index
                break
    else:
        fallback = ("Error:", "Exception", "e: file://")
        indices = [i for i, line in enumerate(lines) if any(marker.lower() in line.lower() for marker in fallback)]
        start = indices[0] if indices else max(0, len(lines) - 30)
    candidates = lines[start:start + 30]
    cleaned = [sanitize(line) for line in candidates if sanitize(line)]
    return cleaned[:18]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--code", type=int, required=True)
    parser.add_argument("--log", action="append", default=[])
    parser.add_argument("--preflight")
    parser.add_argument("--project-discovery")
    parser.add_argument("--project-prepare")
    parser.add_argument("--auto-fixes")
    parser.add_argument("--adaptive-fixes")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    log_parts: list[tuple[Path, str]] = []
    for value in args.log:
        path = Path(value)
        if path.is_file():
            log_parts.append((path, path.read_text(encoding="utf-8", errors="replace")))
    text = "\n".join(content for _, content in log_parts)
    primary_text = text
    primary_log = None
    for path, content in reversed(log_parts):
        if re.search(r"Error:|FAILURE:|BUILD FAILED|Exception|e: file://", content, re.I):
            primary_text = content
            primary_log = path.name
            break
    category = "unknown"
    title = "خطای نامشخص Build"
    cause = "فرآیند Build متوقف شد اما الگوی خطا هنوز در دسته‌بندی‌های شناخته‌شده ثبت نشده است."
    solution = "جزئیات فنی زیر و Workflow Run را بررسی کنید؛ برای توسعه موتور تشخیص، همین گزارش کافی است."
    for candidate_text in (primary_text, text):
        matched = False
        for rule_category, patterns, rule_title, rule_cause, rule_solution in RULES:
            if any(re.search(pattern, candidate_text, re.I | re.M) for pattern in patterns):
                category, title, cause, solution = rule_category, rule_title, rule_cause, rule_solution
                matched = True
                break
        if matched:
            break
    preflight = {}
    if args.preflight and Path(args.preflight).is_file():
        preflight = json.loads(Path(args.preflight).read_text(encoding="utf-8"))
    project_discovery = {}
    if args.project_discovery and Path(args.project_discovery).is_file():
        project_discovery = json.loads(Path(args.project_discovery).read_text(encoding="utf-8"))
    project_prepare = {}
    if args.project_prepare and Path(args.project_prepare).is_file():
        project_prepare = json.loads(Path(args.project_prepare).read_text(encoding="utf-8"))
    fix_payloads = []
    for value, fallback in (
        (args.auto_fixes, "auto-fixes.json"),
        (args.adaptive_fixes, "adaptive-fixes.json"),
    ):
        path = Path(value) if value else args.output.parent / fallback
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            fix_payloads.append(payload)
    applied_fixes = [item for payload in fix_payloads for item in payload.get("applied", [])]
    fix_attempts = [item for payload in fix_payloads for item in payload.get("attempts", [])]
    if applied_fixes:
        solution = (
            solution
            + " یک یا چند اصلاح سازگاری فقط در Workspace موقت اعمال شد، "
              "اما آخرین تلاش با خطای نهایی بالا متوقف شد."
        )
    excerpt = select_excerpt(primary_text.splitlines())
    report = {
        "category": category,
        "title": title,
        "stage": args.stage,
        "exit_code": args.code,
        "cause": cause,
        "result": RESULT_BY_CATEGORY.get(category, "ایراد اصلی از سورس یا محیط Build است و برای تعیین دقیق‌تر باید اولین خطای واقعی بررسی شود."),
        "solution": solution,
        "technical_excerpt": excerpt,
        "java_version": preflight.get("java_version"),
        "gradle_version": preflight.get("gradle_version"),
        "flavors": preflight.get("flavors", []),
        "fallback_signing_used": bool(preflight.get("fallback_signing_used", False)),
        "signing_reason": preflight.get("signing_reason"),
        "primary_log": primary_log,
        "auto_fixes": applied_fixes,
        "auto_fix_attempts": fix_attempts,
        "project_discovery": {
            "candidate_count": project_discovery.get("candidate_count"),
            "ambiguous": project_discovery.get("ambiguous"),
            "selected": project_discovery.get("selected"),
        } if project_discovery else None,
        "project_prepare": {
            "mode": project_prepare.get("mode"),
            "android_generated": project_prepare.get("android_generated"),
            "overlay_path": project_prepare.get("overlay_path"),
            "overlay_merged": project_prepare.get("overlay_merged"),
            "actions": project_prepare.get("actions", []),
            "error": project_prepare.get("error"),
        } if project_prepare else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
