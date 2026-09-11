"""Durable, explicitly reviewed manual chapters; no model calls or budget refunds."""
import hashlib
import json
import logging
from pathlib import Path
import time


class ManualInputRequired(RuntimeError):
    pass


def pending_request(store, cfg, book, number):
    from .translation import source_input_hash
    return store.db.execute("SELECT * FROM manual_requests WHERE book_id=? AND chapter=? "
        "AND source_hash=? AND style_revision=? AND state='pending' ORDER BY id LIMIT 1",
        (book['id'], number, source_input_hash(store, book['id'], number),
         cfg['agent'].get('style_revision', 'legacy'))).fetchone()


def require_manual(store, cfg, book, number, stage, call_id):
    from .translation import source_input_hash, relevant_glossary
    if stage not in ('translate', 'edit'):
        raise ValueError('Invalid manual stage')
    digest = source_input_hash(store, book['id'], number)
    revision = cfg['agent'].get('style_revision', 'legacy')
    root = (cfg['database'].parent / 'manual-review' / book['id'] /
            f'chapter-{number:04}-{stage}-{digest[:12]}-{revision}').resolve()
    path = root / 'submission.json'
    store.db.execute('INSERT OR IGNORE INTO manual_requests '
        '(book_id,chapter,stage,source_hash,style_revision,call_id,path,created) VALUES(?,?,?,?,?,?,?,?)',
        (book['id'], number, stage, digest, revision, call_id, str(path), time.time()))
    ch = store.db.execute('SELECT * FROM chapters WHERE book_id=? AND number=?', (book['id'], number)).fetchone()
    root.mkdir(parents=True, exist_ok=True)
    source = json.loads(ch['source'])
    glossary = relevant_glossary(store, book['id'], json.loads(book['glossary'].read_text(encoding='utf-8')),
                                '\n'.join(source), number)
    draft = json.loads(ch['translated']) if ch['translated'] else None
    result = draft or {'title': '', 'continuity': '', 'issues': [], 'terms': [],
                      'source_notes': [], 'glossary_readings': [],
                      'paragraphs': [{'id': n, 'text': '', 'join_previous': False}
                                     for n in range(1, len(source) + 1)]}
    submission = {'book_id': book['id'], 'chapter': number, 'stage': stage,
                  'source_hash': digest, 'style_revision': revision, 'ready': False,
                  'reviewed': False, 'result': result}
    files = {'submission.json': json.dumps(submission, ensure_ascii=False, indent=2),
             'source.json': json.dumps({'title': ch['title'], 'glossary': glossary,
                 'paragraphs': [{'id': n, 'text': p} for n, p in enumerate(source, 1)]}, ensure_ascii=False, indent=2),
             'README.txt': 'Điền bản tiếng Việt hoàn chỉnh vào result trong submission.json.\n'
                 'Giữ nguyên metadata, đủ ID đoạn, tên Hán–Việt và số liệu; đọc nguyên tác trong source.json.\n'
                 'Đây là bản do bạn dịch và biên tập cuối cùng, không gửi lại agent. Điền title, continuity và các đoạn.\n'
                 'Dùng join_previous cho câu bị ngắt, giữ xưng hô Hán–Việt; không thêm ghi chú vào lời văn.\n'
                 'Sau khi tự đọc và biên tập xong, đặt ready=true và reviewed=true, rồi chạy start.cmd.\n'
                 'Bản nhập thiếu/sai cấu trúc sẽ bị từ chối và ghi rõ trong status.txt; dữ liệu cũ được giữ nguyên.\n'}
    for name, content in files.items():
        try:
            with (root / name).open('x', encoding='utf-8') as handle:
                handle.write(content)
        except FileExistsError:
            pass # Never overwrite the user's partially completed submission.
    store.db.execute('UPDATE chapters SET state=?,error=? WHERE book_id=? AND number=? AND edited IS NULL',
                     (stage + '_manual', 'Both models declined; manual input: ' + str(path), book['id'], number))
    return path


def import_submission(store, cfg, path):
    from .translation import (validate_result, relevant_glossary, load_rendering_aliases,
                              save_terms, source_input_hash)
    path = Path(path).resolve()
    request = store.db.execute('SELECT * FROM manual_requests WHERE path=?', (str(path),)).fetchone()
    if not request:
        raise ValueError('No registered manual request for this file')
    if path.stat().st_size > cfg['agent']['max_output_bytes']:
        raise ValueError('Manual submission exceeds chapter byte limit')
    raw = path.read_text(encoding='utf-8-sig')
    digest = hashlib.sha256(raw.encode()).hexdigest()
    if request['state'] == 'imported':
        if digest == request['submission_hash']:
            return 0
        raise ValueError('An imported chapter cannot be overwritten by another submission')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('Manual submission must be a JSON object')
    if data.get('ready') is not True or data.get('reviewed') is not True:
        raise ValueError('Set ready=true and reviewed=true only after manual translation and editing are complete')
    for key in ('book_id', 'chapter', 'stage', 'source_hash', 'style_revision'):
        if type(data.get(key)) is not type(request[key]) or data.get(key) != request[key]:
            raise ValueError('Manual submission metadata changed: ' + key)
    book = next(b for b in cfg['books'] if b['id'] == request['book_id'])
    if not book.get('enabled', True):
        raise ValueError('Book is disabled')
    if (request['source_hash'] != source_input_hash(store, book['id'], request['chapter']) or
            request['style_revision'] != cfg['agent'].get('style_revision', 'legacy')):
        raise ValueError('Source/style changed; manual submission is stale')
    ch = store.db.execute('SELECT * FROM chapters WHERE book_id=? AND number=?',
                         (book['id'], request['chapter'])).fetchone()
    if ch['edited'] or store.db.execute("SELECT 1 FROM calls WHERE book_id=? AND chapter=? AND status='reserved'",
                                        (book['id'], ch['number'])).fetchone():
        raise ValueError('Chapter already completed or has an unresolved agent call')
    glossary = json.loads(book['glossary'].read_text(encoding='utf-8'))
    locked = store.db.execute('SELECT glossary_hash FROM books WHERE id=?', (book['id'],)).fetchone()[0]
    if locked and locked != hashlib.sha256(json.dumps(glossary, sort_keys=True, ensure_ascii=False).encode()).hexdigest():
        raise ValueError('Glossary changed; reconcile before importing')
    source = json.loads(ch['source'])
    glossary = relevant_glossary(store, book['id'], glossary, '\n'.join(source), ch['number'])
    from .glossary_review import approved_readings
    result = validate_result(json.dumps(data['result'], ensure_ascii=False), source, glossary,
                             rendering_aliases=load_rendering_aliases(book),
                             reviewed_readings=approved_readings(store, book['id'], ch['number'], source))
    accepted = json.dumps(result, ensure_ascii=False)
    with store.transaction():
        save_terms(store, book['id'], ch['number'], result, approved=True)
        store.db.execute("UPDATE chapters SET translated=COALESCE(translated,?),edited=?,summary=?,state='edited',error=NULL "
                         'WHERE book_id=? AND number=?', (accepted, accepted, result['continuity'], book['id'], ch['number']))
        store.db.execute("UPDATE manual_requests SET state='imported',imported=?,submission_hash=?,submission=?,error=NULL WHERE id=?",
                         (time.time(), digest, raw, request['id']))
        store.db.execute("UPDATE calls SET status='acknowledged' WHERE book_id=? AND chapter=? AND status='failed' "
                         "AND failure_kind='content_blocked'", (book['id'], ch['number']))
    logging.info('%s chapter %s imported as manually translated/edited; no model call', book['id'], ch['number'])
    return 1


def import_ready(store, cfg):
    """Called under the pipeline lock on each manual start."""
    count = 0
    for request in store.db.execute("SELECT * FROM manual_requests WHERE state='pending' ORDER BY id").fetchall():
        try:
            path = Path(request['path'])
            if not path.exists():
                book = next(b for b in cfg['books'] if b['id'] == request['book_id'])
                if pending_request(store, cfg, book, request['chapter']):
                    require_manual(store, cfg, book, request['chapter'], request['stage'], request['call_id'])
                continue
            if path.stat().st_size > cfg['agent']['max_output_bytes']:
                raise ValueError('Manual submission exceeds chapter byte limit')
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict) or data.get('ready') is not True:
                continue
            count += import_submission(store, cfg, path)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            store.db.execute('UPDATE manual_requests SET error=? WHERE id=?', (str(exc)[:1000], request['id']))
            logging.warning('Manual chapter %s not imported: %s', request['chapter'], exc)
    return count
