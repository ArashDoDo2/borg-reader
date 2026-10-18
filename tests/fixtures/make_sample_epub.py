import zipfile,sys
# usage: make_sample_epub.py OUT.epub [TITLE] [IDENTIFIER]
TITLE = sys.argv[2] if len(sys.argv) > 2 else 'کتاب آزمایشی'
IDENT = sys.argv[3] if len(sys.argv) > 3 else 'test-1'
z=zipfile.ZipFile(sys.argv[1],'w')
z.writestr(zipfile.ZipInfo('mimetype'),'application/epub+zip',compress_type=zipfile.ZIP_STORED)
D=zipfile.ZIP_DEFLATED
z.writestr('META-INF/container.xml','<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',D)
z.writestr('OEBPS/content.opf',f'''<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">{IDENT}</dc:identifier><dc:title>{TITLE}</dc:title><dc:creator>نویسنده تست</dc:creator><dc:language>fa</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/></manifest><spine page-progression-direction="rtl"><itemref idref="c1"/><itemref idref="c2"/></spine></package>''',D)
z.writestr('OEBPS/nav.xhtml','<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>فهرست</title></head><body><nav epub:type="toc"><ol><li><a href="c1.xhtml">فصل اول</a></li><li><a href="c2.xhtml">فصل دوم</a></li></ol></nav></body></html>',D)
for i,n in ((1,'اول'),(2,'دوم')):
  p=''.join(f'<p>این پاراگراف شماره {k} از فصل {n} است و برای آزمایش صفحه‌بندی نوشته شده است. متن فارسی باید راست‌به‌چپ نمایش داده شود.</p>' for k in range(60))
  z.writestr(f'OEBPS/c{i}.xhtml',f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="fa" dir="rtl"><head><title>فصل {n}</title></head><body><h1>فصل {n}</h1>{p}</body></html>',D)
z.close()
