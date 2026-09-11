"""Explicit missing chapters are terminal records, never invented translations."""
import json


def finalize_missing(store, book):
    if not book.get('allow_missing_chapters', True):
        return 0
    db = store.db
    sources = book.get('sources', [])
    if not sources:
        return 0
    for source in sources:
        scan = db.execute('SELECT state,attempts FROM source_scans WHERE book_id=? AND source_id=?',
                          (book['id'], source['id'])).fetchone()
        if not scan or (scan['state'] != 'ready' and scan['attempts'] < 3):
            return 0
    done = 0
    with store.transaction():
        for row in db.execute("SELECT * FROM chapters WHERE book_id=? AND state='source_review'",
                              (book['id'],)).fetchall():
            active = {source['id'] for source in sources}
            candidates = db.execute('SELECT source_id,state,attempts FROM source_candidates '
                                    'WHERE book_id=? AND canonical_number=?', (book['id'], row['number']))
            if any(c['source_id'] in active and c['state'] == 'pending' and c['attempts'] < 3 for c in candidates):
                continue
            note = ('Chương bị thiếu: đã kiểm tra tất cả nguồn được cấu hình và hết số lần thử, '
                    'nhưng không tìm được bản đầy đủ có thể xác minh. Không suy đoán nội dung.')
            db.execute("UPDATE chapters SET state='missing',summary=? WHERE book_id=? AND number=?",
                       (note, book['id'], row['number']))
            done += 1
    return done


def is_complete(store, book):
    rows = store.db.execute('SELECT number,state,edited,summary FROM chapters WHERE book_id=? ORDER BY number',
                            (book['id'],)).fetchall()
    return (book['completed'] and len(rows) == book['expected_chapters'] and
            [r['number'] for r in rows] == list(range(1, book['expected_chapters'] + 1)) and
            all((r['state'] == 'edited' and r['edited']) or
                (book.get('allow_missing_chapters', True) and r['state'] == 'missing' and r['summary']) for r in rows))


def chapter_text(row):
    if row['state'] == 'missing':
        return {'title': 'Chương bị thiếu', 'paragraphs': [{'id': 1, 'text': row['summary']}]}
    return json.loads(row['edited'])
