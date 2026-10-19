# آپلود خودکار کتاب به کاتالوگ عمومی — API `api-book`
# Automatic book upload to the public catalog — `api-book`

برنامهٔ خودتان می‌تواند روزی چند EPUB را مستقیم در **کاتالوگ عمومی** بُرگ بگذارد (نه در کتابخانهٔ شخصی کسی).
پیش از ذخیره، قاعده‌های «پاک‌سازی متن» که در پنل مدیر تعریف کرده‌اید روی کتاب اجرا می‌شود.

Your own program can put a few EPUBs a day straight into Borg's **public catalog** (never into anyone's personal
library). Before the book is stored, the server applies the cleanup rules you defined in the admin panel.

## ۱. ساخت کلید / Create a key
پنل مدیر ← «کلیدهای API» ← نام کلید (مثلاً «برنامهٔ آپلود») ← ساخت. کلید (`borg_…`، ۴۵ نویسه) **فقط یک بار** نشان داده
می‌شود؛ آن را در تنظیمات برنامه‌تان نگه دارید (نه در گیت). اگر «نیاز به بازبینی» را بزنید، کتاب‌های این کلید در صف
بازبینی می‌مانند تا تأییدشان کنید. کلیدی که لو رفته را «باطل» کنید و کلید تازه بسازید.

Admin panel → API keys → name → create. The key (`borg_…`, 45 characters) is shown **once**; keep it in your
program's settings, never in a repository. With "require review" the key's books wait in the review queue.
Revoke a leaked key and create a new one. The server stores only a hash of the key.

## ۲. ارسال کتاب / Upload
```sh
curl -H "Authorization: Bearer borg_…" \
     -F file=@book.epub -F category=رمان \
     "https://parsun.com/borg/api/index.php?r=api-book"
```
اگر میزبان سرآیند `Authorization` را حذف کرد، به جایش `-H "X-Borg-Token: borg_…"` بفرستید.
If the host strips the `Authorization` header, send `-H "X-Borg-Token: borg_…"` instead.

| فیلد / field | | |
|---|---|---|
| `file` | الزامی / required | فایل EPUB (`multipart/form-data`) |
| `title` | ≤ 500 | اگر نباشد از OPF، بعد از نام فایل / from the OPF, else the file name |
| `author` | ≤ 300 | اگر نباشد از OPF / from the OPF |
| `language`, `publisher`, `published` | ≤ 20 / 300 / 40 | اگر نباشد از OPF (`dc:date` → `YYYY-MM-DD`) |
| `category` | ≤ 80 | دستهٔ کاتالوگ / catalog category |
| `description` | ≤ 2000 | توضیح در کاتالوگ / catalog description |

جلد از OPF برداشته می‌شود (EPUB 3 `cover-image` یا EPUB 2 `meta name="cover"`؛ PNG/JPEG/WebP/GIF تا ۱٫۵ مگابایت).
The cover is taken from the OPF (EPUB 3 `cover-image` or EPUB 2 `meta name="cover"`; PNG/JPEG/WebP/GIF up to 1.5 MB).

## ۳. پاسخ / Response — `201`
```json
{"book": {"id": 12, "sha256": "…", "size": 123456, "title": "…", "author": "…", "language": "fa",
          "publisher": "…", "published": "2020-03-04", "has_cover": true},
 "catalog_status": "approved", "duplicate": false,
 "cleaned": 5, "cleanup": "applied", "skipped_rules": []}
```
- `catalog_status`: `approved` (منتشر شد) یا `pending` (کلید با بازبینی). کتابی که قبلاً در کاتالوگ است وضعیتش را
  نگه می‌دارد؛ فقط `category`/`description` (اگر فرستاده شوند) به‌روز می‌شود. کتاب ردشده دوباره ثبت می‌شود.
  An entry already in the catalog keeps its status; `category`/`description` are updated when sent.
- `duplicate: true`: همین فایل (پس از پاک‌سازی) قبلاً ذخیره شده بود؛ نسخهٔ دومی ساخته نمی‌شود.
  The same (cleaned) file was already stored; no second copy is made.
- `cleanup`: `applied` (قاعده‌ها اجرا شد)، `no-rules` (قاعده‌ای تعریف نشده)، `unavailable` (سرور ZipArchive/DOM
  ندارد؛ کتاب بدون تغییر ذخیره شد). `cleaned` = تعداد موارد حذف‌شده.
- `skipped_rules`: شناسهٔ قاعده‌هایی که برای این کتاب «بیش از حد کلی» بودند و اجرا نشدند (بیش از ۳۰۰ مورد، یا بیش از
  ۲٪ متن؛ برای «کل خط» ۱۰٪) — مثل پاک‌سازی خودکار هنگام افزودن کتاب در مرورگر.
  Ids of rules judged too broad for this book (> 300 matches, or > 2% of the text; whole-line rules 10%), as in the
  browser's auto-clean on import.

## ۴. پاک‌سازی متن / Cleanup rules
قاعده‌ها از بخش «پاک‌سازی متن» پنل مدیر می‌آیند (`GET/POST clean-rules`): عبارت دقیق، «کل خطِ شامل این متن» و
«لینک‌های تلگرام». سرور همان کاری را می‌کند که مرورگر می‌کند و فایل پاک‌شده ذخیره می‌شود (فایل اصلی نگه داشته
نمی‌شود). تغییر قاعده‌ها روی کتاب‌های قبلی اثری ندارد.
Rules come from the admin's «پاک‌سازی متن» (`GET/POST clean-rules`): exact phrase, whole line containing a text, and
Telegram links. The server does what the browser does and stores the cleaned file (the original is not kept).
Changing the rules does not touch books already stored.

## ۵. خطاها / Errors
همه JSON هستند: `{"error": "<code>", "message": "…"}`.

| کد HTTP | `error` | معنی / meaning |
|---|---|---|
| 401 | `missing_token`, `bad_token` | کلید نیست، اشتباه است یا باطل شده (یا سازنده‌اش دیگر مدیر فعال نیست) |
| 429 | `rate_limited` | بیش از ۶۰ درخواست در ساعت با یک کلید، یا ۳۰ کلید اشتباه در ۱۵ دقیقه از یک IP |
| 415 | `bad_content_type` | بدنه `multipart/form-data` نیست |
| 400 | `missing_file`, `empty_file`, `partial_upload` | فایل نرسید / خالی / ناقص — دوباره بفرستید |
| 400 | `not_epub` | فایل EPUB معتبر نیست |
| 400 | `too_long_<field>`, `bad_<field>` | فیلدی بلندتر از حد یا نامعتبر |
| 413 | `too_large` | بزرگ‌تر از حد کتاب در تنظیمات سرور (`max_book_bytes`، پیش‌فرض ۱۰۰ مگابایت) |
| 413 | `file_too_large_for_php`, `post_too_large` | بزرگ‌تر از `upload_max_filesize` / `post_max_size` در PHP میزبان (مقدار در پیام هست) |
| 500 | `upload_failed`, `storage`, `server_error` | مشکل سرور؛ بعداً دوباره |

## ۶. محدودیت‌ها / Limits
- ۶۰ درخواست در ساعت برای هر کلید (درخواست‌های ناموفق هم حساب می‌شوند). 60 requests per key per hour.
- اندازهٔ فایل: کمترینِ `max_book_bytes` و محدودیت‌های PHP میزبان (`upload_max_filesize`, `post_max_size`)؛
  مقدار فعلی در `GET ?r=health` زیر `limits` دیده می‌شود. روی هاست اشتراکی این دو معمولاً از cPanel ← «MultiPHP INI
  Editor» قابل تغییرند. File size: the smallest of `max_book_bytes` and the host's PHP limits (see `?r=health`).
- برای خطاهای 429 و 5xx چند دقیقه صبر کنید و دوباره بفرستید؛ ارسال دوباره امن است (تکراری‌ها یکی می‌شوند).
  On 429/5xx wait and retry; retrying is safe (duplicates collapse into one book).
