"""Export saved edited chapters for local quality review, outside the EPUB outbox."""
import argparse
from datetime import datetime
import html
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', default='data/library.db')
    parser.add_argument('--book', default='han-men-bai-jia-zi')
    parser.add_argument('--chapters', nargs='+', type=int, default=[1, 8, 12])
    parser.add_argument('--output', default='data/review/ban-dich-mau.html')
    args = parser.parse_args()
    with sqlite3.connect(args.database) as db:
        book = db.execute('SELECT title FROM books WHERE id=?', (args.book,)).fetchone()
        sections, markdown, counts = [], [], {}
        for number in args.chapters:
            row = db.execute('SELECT source,edited FROM chapters WHERE book_id=? AND number=?',
                             (args.book, number)).fetchone()
            if not row or not row[1]:
                raise ValueError(f'Chapter {number} has no edited result')
            source, result = json.loads(row[0]), json.loads(row[1])
            if len(source) != len(result['paragraphs']):
                raise ValueError('Paragraph coverage differs')
            title = f"Chương {number} — {result['title']}"
            markdown += [f'## {title}', '', *[p['text'] + '\n' for p in result['paragraphs']]]
            rows = []
            for original, p in zip(source, result['paragraphs']):
                rows.append(f'<div class="pair"><p class="zh" lang="zh">{html.escape(original)}</p>'
                            f'<p class="vi"><small>{p["id"]}</small>{html.escape(p["text"])}</p></div>')
            sections.append(f'<section id="ch-{number}"><h2>{html.escape(title)}</h2>{"".join(rows)}</section>')
            counts[number] = len(source)
    nav = ' · '.join(f'<a href="#ch-{n}">Chương {n}</a>' for n in args.chapters)
    document = '''<!doctype html><html lang="vi"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bản dịch mẫu để đánh giá</title><style>
body{margin:2rem auto;padding:0 1rem;max-width:1100px;background:#faf8f3;color:#25231f;font:19px/1.75 Georgia,"Times New Roman",serif}
header{font-family:system-ui;font-size:16px}h1{font-size:1.7rem}h2{font-size:1.4rem;line-height:1.5}
nav{padding:1rem 0}a{color:#325e71}section{margin:4rem 0}p{margin:.45em 0;text-indent:1.2em}
.pair{display:grid;grid-template-columns:1fr;gap:0 2rem}.zh{display:none;color:#666;font-size:17px}
.vi{max-width:42em}small{display:inline-block;width:2em;font:11px system-ui;color:#999;text-indent:0}
#bilingual:checked~main .pair{grid-template-columns:1fr 1fr}#bilingual:checked~main .zh{display:block}
@media(max-width:700px){#bilingual:checked~main .pair{grid-template-columns:1fr}.zh{border-top:1px solid #ddd}}
</style><header><h1>''' + html.escape(book[0]) + '''</h1>
<p>Các chương đã qua biên tập, trích nguyên văn từ dữ liệu đang lưu. Bản đọc thử để đánh giá; chưa phải EPUB cuối và không gửi Kindle.</p>
<p>Xuất lúc ''' + datetime.now().isoformat(timespec='seconds') + '</p><nav>' + nav + '''</nav></header>
<input type="checkbox" id="bilingual"><label for="bilingual"> Hiện nguyên tác Trung Quốc để đối chiếu từng đoạn</label>
<main>''' + ''.join(sections) + '</main></html>'
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding='utf-8')
    output.with_suffix('.md').write_text('# Bản dịch mẫu — nguyên văn bản biên tập\n\n' + '\n'.join(markdown), encoding='utf-8')
    print(json.dumps({'path': str(output.resolve()), 'paragraph_counts': counts}, ensure_ascii=False))


if __name__ == '__main__':
    main()
