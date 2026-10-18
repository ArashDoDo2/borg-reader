# بُرگ (Borg)

کتاب‌خوان فارسی EPUB به‌صورت PWA؛ راست‌به‌چپ، آفلاین، و بدون ارسال کتاب به سرور.
نسخهٔ منتشرشده: https://parsun.com/borg/ — نسخهٔ آزمایشی: https://parsun.com/borg-test/

## ساختار
- `src/` — فایل‌هایی که روی سرور منتشر می‌شوند (HTML/CSS/JS، epub.js، JSZip و فونت Vazirmatn به‌صورت محلی).
- `tests/` — آزمون‌های خودکار Playwright و فایل‌های EPUB نمونه.
- `scripts/` — انتشار در cPanel، آزمون دود سرور، ساخت ZIP.
- `docs/` — معماری، تصمیم‌ها (ADR) و سند محصول.

## اجرای آزمون‌ها
```bash
pip install playwright && python3 -m playwright install chromium
python3 tests/e2e_test.py
```

## روال انتشار
1. تغییرات روی شاخهٔ `staging` push شوند و آزمون‌ها سبز باشند.
2. cPanel ← Git Version Control ← Manage ← Pull or Deploy: شاخهٔ `staging` را checkout، «Update from Remote» و سپس «Deploy HEAD Commit». نتیجه در `/borg-test/` منتشر می‌شود.
3. `scripts/smoke-test.sh https://parsun.com/borg-test/ <نسخه>` و بررسی دستی روی گوشی.
4. ادغام `staging` در `main`، همان مراحل با شاخهٔ `main` (انتشار در `/borg/`)، آزمون دود، و tag نسخه.

بازگشت: checkout tag نسخهٔ قبل روی شاخهٔ `main` با `CACHE_NAME` بالاتر، و Deploy دوباره. دادهٔ کاربران (IndexedDB) دست نمی‌خورد.

## مجوزها
epub.js (BSD-2-Clause)، JSZip (MIT یا GPLv3)، Vazirmatn (SIL OFL 1.1) — فایل مجوز هرکدام کنار خودش در `src/assets/` است.
