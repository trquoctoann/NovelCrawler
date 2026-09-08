"""Build an original tiny sample EPUB offline; no crawl, model calls or email."""
import json
from pathlib import Path

from novel_crawler.epub import export_book
from novel_crawler.store import Store


def main():
    root = Path(__file__).resolve().parents[1]
    store = Store(root / 'data' / 'demo.db')
    book = {'id': 'demo-dem-o-lang', 'title': 'Đêm ở làng — Sách mẫu kiểm tra',
            'author': 'Văn bản mẫu của dự án', 'expected_chapters': 2,
            'source_url': 'urn:novel:original-demo', 'completed': True}
    store.register(book)
    samples = [
        ('Ánh đèn bên cửa', ['Mưa vừa tạnh. Dưới mái hiên, ngọn đèn dầu hắt một quầng sáng nhỏ xuống sân gạch.',
                             '“Ngày mai chúng ta đi sớm nhé,” nàng nói.',
                             'Chàng gật đầu, khép lại cuốn sổ đã sờn mép. Ngoài ngõ, tiếng guốc xa dần trong đêm.']),
        ('Buổi sớm', ['Khi sương còn đọng trên lá tre, hai người đã ra đến đầu làng.',
                      'Con đường phía trước uốn quanh triền đồi. Chàng ngoảnh lại một lần, rồi cùng nàng bước tiếp.',
                      'Đây là văn bản tự viết để kiểm tra dấu tiếng Việt, thụt đầu dòng và mục lục; không phải bản dịch của Hàn Môn Bại Gia Tử.']),
    ]
    try:
        for n, (title, paragraphs) in enumerate(samples, 1):
            data = json.dumps({'title': title, 'paragraphs': [{'id': i, 'text': t} for i, t in enumerate(paragraphs, 1)],
                               'continuity': 'Văn bản mẫu.', 'issues': []}, ensure_ascii=False)
            store.db.execute('INSERT OR REPLACE INTO chapters(book_id,number,url,title,source,translated,edited,state) '
                             'VALUES(?,?,?,?,?,?,?,?)',
                             (book['id'], n, f'urn:demo:{n}', title, json.dumps(paragraphs, ensure_ascii=False), data, data, 'edited'))
        print(export_book(store, book, root / 'data' / 'epub'))
    finally:
        store.close()


if __name__ == '__main__':
    main()
