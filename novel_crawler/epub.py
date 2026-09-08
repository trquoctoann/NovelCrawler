"""Reflowable EPUB 3: grouped XHTML, one TOC entry and anchor per chapter."""
from datetime import datetime, timezone
from html import escape
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED


CSS = '''body { font-family: serif; margin: 0; line-height: 1.4; }
p { text-indent: 1.2em; margin: 0; text-align: justify; widows: 2; orphans: 2; }
h1, h2 { text-align: center; text-indent: 0; margin: 1.2em 0; }
section { break-before: page; page-break-before: always; }
nav li { margin-bottom: .35em; }
'''


def xhtml(title, content):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            'xml:lang="vi" lang="vi"><head><title>' + escape(title) +
            '</title><link rel="stylesheet" type="text/css" href="style.css"/></head><body>' +
            content + '</body></html>')


def export_book(store, book, output_dir, preview=False):
    rows = store.db.execute('SELECT * FROM chapters WHERE book_id=? ORDER BY number', (book['id'],)).fetchall()
    complete = (book['completed'] and len(rows) == book['expected_chapters'] and
                [r['number'] for r in rows] == list(range(1, book['expected_chapters'] + 1)) and
                all(r['state'] == 'edited' and r['edited'] for r in rows))
    if not preview and not complete:
        raise ValueError('Final export requires a completed book with every chapter edited')
    if preview:
        contiguous = []
        for n, row in enumerate(rows, 1):
            if row['number'] != n or row['state'] != 'edited':
                break
            contiguous.append(row)
        rows = contiguous
    if not rows:
        raise ValueError('No contiguous edited chapters to export')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (book['id'] + ('.preview' if preview else '') + '.epub')
    temp = path.with_suffix('.epub.tmp')
    uid = 'urn:novel:' + book['id']
    files = {'style.css': CSS}
    toc, manifest, spine = [], [], []
    group_size = max(20, math.ceil(len(rows) / 200))
    for group_index in range(0, len(rows), group_size):
        name = f'part-{group_index // group_size + 1:04}.xhtml'
        sections = []
        for row in rows[group_index:group_index + group_size]:
            edited = json.loads(row['edited'])
            label = f"Chương {row['number']}: {edited['title']}"
            anchor = f"ch-{row['number']}"
            sections.append(f'<section id="{anchor}"><h2>{escape(label)}</h2>' +
                            ''.join('<p>' + escape(p['text']) + '</p>' for p in edited['paragraphs']) + '</section>')
            toc.append(f'<li><a href="{name}#{anchor}">{escape(label)}</a></li>')
        files[name] = xhtml(book['title'], ''.join(sections))
        if len(files[name].encode()) > 25_000_000:
            raise ValueError('XHTML part too large')
        item_id = f'part{group_index}'
        manifest.append(f'<item id="{item_id}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{item_id}"/>')
    files['nav.xhtml'] = xhtml('Mục lục', '<nav epub:type="toc" id="toc"><h1>Mục lục</h1><ol>' + ''.join(toc) + '</ol></nav>')
    modified = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    opf = ('<?xml version="1.0" encoding="utf-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="book-id">{uid}</dc:identifier><dc:title>{escape(book["title"])}</dc:title>'
           f'<dc:creator>{escape(book["author"])}</dc:creator><dc:language>vi</dc:language>'
           f'<dc:source>{escape(book["source_url"])}</dc:source><meta property="dcterms:modified">{modified}</meta>'
           '</metadata><manifest><item id="css" href="style.css" media-type="text/css"/>'
           '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' +
           ''.join(manifest) + '</manifest><spine>' + ''.join(spine) + '</spine></package>')
    container = ('<?xml version="1.0" encoding="utf-8"?>'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')
    with ZipFile(temp, 'w', compression=ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=ZIP_STORED)
        z.writestr('META-INF/container.xml', container)
        z.writestr('OEBPS/content.opf', opf)
        for name, content in files.items():
            z.writestr('OEBPS/' + name, content)
    verify_epub(temp)
    temp.replace(path)
    return path


def verify_epub(path):
    with ZipFile(path) as z:
        first = z.infolist()[0]
        if first.filename != 'mimetype' or first.compress_type != ZIP_STORED or z.read(first) != b'application/epub+zip':
            raise ValueError('Invalid EPUB mimetype')
        if z.testzip():
            raise ValueError('Corrupt EPUB archive')
        for name in z.namelist():
            if name.endswith(('.xml', '.opf', '.xhtml')):
                ET.fromstring(z.read(name))
        ns = {'o': 'http://www.idpf.org/2007/opf', 'x': 'http://www.w3.org/1999/xhtml'}
        opf = ET.fromstring(z.read('OEBPS/content.opf'))
        items = {i.attrib['id']: i.attrib['href'] for i in opf.findall('o:manifest/o:item', ns)}
        for href in items.values():
            if 'OEBPS/' + href not in z.namelist():
                raise ValueError('Missing manifest target')
        for item in opf.findall('o:spine/o:itemref', ns):
            if item.attrib['idref'] not in items:
                raise ValueError('Broken spine')
        nav = ET.fromstring(z.read('OEBPS/nav.xhtml'))
        for a in nav.findall('.//x:a', ns):
            name, anchor = a.attrib['href'].split('#')
            doc = ET.fromstring(z.read('OEBPS/' + name))
            if not any(node.attrib.get('id') == anchor for node in doc.iter()):
                raise ValueError('Broken chapter TOC anchor')
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
