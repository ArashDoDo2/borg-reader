# Builds sample-notes.epub: a chapter with an EPUB3 footnote reference, a classic "#fn1" footnote, and an image.
# usage: make_notes_epub.py OUT.epub
import struct, sys, zipfile, zlib

def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

out = sys.argv[1]
z = zipfile.ZipFile(out, "w")
z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
D = zipfile.ZIP_DEFLATED
z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', D)
z.writestr("OEBPS/content.opf", '''<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">notes-1</dc:identifier><dc:title>کتاب پانویس‌دار</dc:title><dc:creator>نویسنده تست</dc:creator><dc:language>fa</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/><item id="img" href="pic.png" media-type="image/png"/></manifest><spine page-progression-direction="rtl"><itemref idref="c1"/><itemref idref="notes"/></spine></package>''', D)
z.writestr("OEBPS/nav.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>فهرست</title></head><body><nav epub:type="toc"><ol><li><a href="c1.xhtml">متن</a></li><li><a href="notes.xhtml">یادداشت‌ها</a></li></ol></nav></body></html>', D)
z.writestr("OEBPS/c1.xhtml", '''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="fa" dir="rtl"><head><title>متن</title></head><body>
<h1>متن اصلی</h1>
<p id="p1">این جمله یک پانویس دارد<a id="r1" epub:type="noteref" href="notes.xhtml#n1"><sup>۱</sup></a> و ادامه پیدا می‌کند. سال 1404 است.</p>
<p id="p2">این جمله پانویس قدیمی دارد<sup><a id="r2" href="#fn2">2</a></sup> که در همین فایل است.</p>
<p><img id="pic" src="pic.png" alt="تصویر آزمایشی" style="width:200px;height:150px"/></p>
<p>پاراگراف آخر.</p>
<p id="fn2">2. این پانویس دوم در همین فایل است.</p>
</body></html>''', D)
z.writestr("OEBPS/notes.xhtml", '''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="fa" dir="rtl"><head><title>یادداشت‌ها</title></head><body>
<aside epub:type="footnote" id="n1"><p>این متن پانویس اول است. <a href="c1.xhtml#r1">↩</a></p></aside>
</body></html>''', D)
z.writestr("OEBPS/pic.png", png(200, 150, (40, 120, 90)), zipfile.ZIP_STORED)
z.close()
