# آپلود خودکار کتاب به کاتالوگ عمومی — API `api-book`
# Automatic book upload to the public catalog — `api-book`

برنامهٔ خودتان می‌تواند روزی چند EPUB را مستقیم در **کاتالوگ عمومی** بُرگ بگذارد (نه در کتابخانهٔ شخصی کسی).
پیش از ذخیره، قاعده‌های «پاک‌سازی متن» که در پنل مدیر تعریف کرده‌اید روی کتاب اجرا می‌شود.

Your own program can put a few EPUBs a day straight into Borg's **public catalog** (never into anyone's personal
library). Before the book is stored, the server applies the cleanup rules you defined in the admin panel.

**PDF هم پذیرفته می‌شود** (مجله‌های متنی فارسی و انگلیسی، بدون تبدیل): نوع فایل از خود بایت‌ها تشخیص داده می‌شود
(`%PDF-` در ۱۰۲۴ بایت اول و `%%EOF` در ۲ کیلوبایت آخر). روی PDF **پاک‌سازی متن اجرا نمی‌شود** و فایل بی‌تغییر ذخیره
می‌شود؛ جلد هم در سرور ساخته نمی‌شود (مرورگر بعداً جلد را می‌سازد). PDF باید متنی باشد (PDF اسکن‌شده خوانده می‌شود ولی
متنی برای جست‌وجو ندارد).
**PDFs are accepted too** (text-based English/Persian magazines, opened directly, no conversion). The type is detected
from the bytes (`%PDF-` within the first 1024 bytes and `%%EOF` within the last 2 KB). PDFs get **no text cleanup** and
are stored unchanged; no cover is made on the server (the app renders one later). Send text-based PDFs; a scanned PDF
opens but has no text to search.

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
# PDF: the same request
curl -H "Authorization: Bearer borg_…" -F file=@issue-12.pdf -F category=مجله \
     "https://parsun.com/borg/api/index.php?r=api-book"
```
اگر میزبان سرآیند `Authorization` را حذف کرد، به جایش `-H "X-Borg-Token: borg_…"` بفرستید.
If the host strips the `Authorization` header, send `-H "X-Borg-Token: borg_…"` instead.

| فیلد / field | | |
|---|---|---|
| `file` | الزامی / required | فایل EPUB یا PDF (`multipart/form-data`) / EPUB or PDF file |
| `title` | ≤ 500 | اگر نباشد از OPF (PDF: `Title` در Info)، بعد از نام فایل / from the OPF (PDF: Info `Title`), else the file name |
| `author` | ≤ 300 | اگر نباشد از OPF (PDF: `Author` در Info) / from the OPF (PDF: Info `Author`) |
| `language`, `publisher`, `published` | ≤ 20 / 300 / 40 | اگر نباشد از OPF (`dc:date` → `YYYY-MM-DD`)؛ برای PDF فقط از درخواست / PDF: request only |
| `category` | ≤ 2000 | دسته‌های کاتالوگ (پایین را ببینید) / catalog categories (see below) |
| `description` | ≤ 2000 | توضیح در کاتالوگ / catalog description |

**دسته‌ها / Categories.** دسته‌ها دو سطح دارند (دسته ← زیردسته) و هر کتاب می‌تواند چند دسته (حداکثر ۱۰) داشته باشد.
در `category` چند مسیر را با `;` یا خط تازه جدا کنید؛ هر مسیر `دسته/زیردسته` یا فقط `دسته` است (`›` و `>` هم جداکننده‌اند،
فاصله‌های اطراف حذف می‌شود). دسته‌ای که هنوز نیست ساخته می‌شود (اول دستهٔ بالا). بیش از دو سطح → ۴۰۰ `too_deep`، بیش از
۱۰ دسته → ۴۰۰ `too_many_categories`، نام بیش از ۸۰ نویسه → ۴۰۰ `too_long_category`.
Categories have two levels (category → subcategory); a book may have several (at most 10). Put one or more paths in
`category`, separated by `;` or newlines, each `Parent/Child` or just `Parent` (`›` and `>` also separate levels; spaces
around names are trimmed). Missing categories are created (parent first). More than two levels → 400 `too_deep`, more
than 10 → 400 `too_many_categories`, a name over 80 characters → 400 `too_long_category`. For a book already in the
catalog, a non-empty `category` replaces its categories.
```sh
curl -H "Authorization: Bearer borg_…" -F file=@book.epub \
     -F "category=ادبیات/رمان; تاریخ › ایران" \
     "https://parsun.com/borg/api/index.php?r=api-book"
```

جلد از OPF برداشته می‌شود (EPUB 3 `cover-image` یا EPUB 2 `meta name="cover"`؛ PNG/JPEG/WebP/GIF تا ۱٫۵ مگابایت).
The cover is taken from the OPF (EPUB 3 `cover-image` or EPUB 2 `meta name="cover"`; PNG/JPEG/WebP/GIF up to 1.5 MB).
برای PDF عنوان و نویسنده در صورت امکان از دیکشنری Info خوانده می‌شود (رشتهٔ ساده یا UTF-16BE؛ اگر Info در «object stream»
فشرده یا PDF رمزدار باشد، خوانده نمی‌شود و نام فایل عنوان می‌شود). بهتر است `title`/`author` را خودتان بفرستید.
For a PDF, title and author come from its Info dictionary when that is cheap to read (plain or UTF-16BE strings; not from
compressed object streams or encrypted files, then the file name is the title). Sending `title`/`author` is safest.

## ۳. پاسخ / Response — `201`
```json
{"book": {"id": 12, "sha256": "…", "size": 123456, "title": "…", "author": "…", "language": "fa",
          "publisher": "…", "published": "2020-03-04", "has_cover": true, "format": "epub"},
 "catalog_status": "approved", "duplicate": false,
 "cleaned": 5, "cleanup": "applied", "skipped_rules": []}
```
- `catalog_status`: `approved` (منتشر شد) یا `pending` (کلید با بازبینی). کتابی که قبلاً در کاتالوگ است وضعیتش را
  نگه می‌دارد؛ فقط `category` (جایگزین همهٔ دسته‌ها) و `description` (اگر فرستاده شوند) به‌روز می‌شود. کتاب ردشده دوباره ثبت می‌شود.
  An entry already in the catalog keeps its status; `category` (replaces all its categories) and `description` are updated when sent.
- `duplicate: true`: همین فایل (پس از پاک‌سازی) قبلاً ذخیره شده بود؛ نسخهٔ دومی ساخته نمی‌شود.
  The same (cleaned) file was already stored; no second copy is made.
- `book.format`: `epub` یا `pdf` (از بایت‌های فایل تشخیص داده می‌شود). Detected from the file's bytes.
- `cleanup`: `applied` (قاعده‌ها اجرا شد)، `no-rules` (قاعده‌ای تعریف نشده)، `unavailable` (سرور ZipArchive/DOM
  ندارد؛ کتاب بدون تغییر ذخیره شد)، `not-applicable` (PDF؛ پاک‌سازی ندارد، `cleaned: 0`). `cleaned` = تعداد موارد حذف‌شده.
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
| 400 | `not_epub` | فایل نه EPUB معتبر است نه PDF / neither a valid EPUB nor a PDF |
| 400 | `not_pdf` | با `%PDF-` شروع می‌شود ولی ناقص است (`%%EOF` در انتها نیست) / starts like a PDF but is truncated |
| 400 | `too_long_<field>`, `bad_<field>` | فیلدی بلندتر از حد یا نامعتبر |
| 400 | `too_deep`, `too_many_categories` | دسته با بیش از دو سطح / بیش از ۱۰ دسته — more than two levels / more than 10 categories |
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
