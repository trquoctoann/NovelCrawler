"""Small deterministic bridge for the authenticated Gmail connector in Codex.

No credentials, email sending, or model calls in this module. Claim before send;
an uncertain outcome stays blocked rather than producing duplicate Kindle books.
"""
import argparse
import base64
import json
import hashlib
from pathlib import Path

from novel_crawler.config import load
from novel_crawler.store import Store, pipeline_lock


def entry(store, cfg, book_id):
    if not cfg['kindle']['enabled'] or cfg['kindle'].get('transport') != 'gmail_connector':
        raise ValueError('Gmail delivery disabled')
    if book_id not in {b['id'] for b in cfg['books']}:
        raise ValueError('Unknown book')
    data = json.loads((store.path.parent / 'outbox' / (book_id + '.json')).read_text(encoding='utf-8'))
    expected = (store.path.parent / 'outbox' / (book_id + '.epub')).resolve()
    if Path(data['path']).resolve() != expected:
        raise ValueError('Outbox snapshot changed')
    # The snapshot passed full EPUB verification when queued. Hash the bytes here;
    # reparsing 1597 chapter anchors for every attachment chunk would be quadratic.
    with expected.open('rb') as f:
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    if digest != data['sha256']:
        raise ValueError('Outbox snapshot changed')
    if (data['from_address'] != cfg['kindle']['sender'] or data['to'] != cfg['kindle']['recipient']
            or data['book_id'] != book_id or data['size'] != expected.stat().st_size):
        raise ValueError('Outbox routing/size changed')
    row = store.db.execute('SELECT * FROM delivery WHERE book_id=?', (book_id,)).fetchone()
    if row is None or row['sha256'] != data['sha256']:
        raise ValueError('Outbox ledger mismatch')
    return data, row


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['status', 'claim', 'chunk', 'ack', 'unknown'])
    p.add_argument('--book')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--message-id')
    args = p.parse_args(argv)
    cfg = load('config.toml')
    with pipeline_lock(cfg['database']):
        store = Store(cfg['database'])
        try:
            if args.action == 'status':
                print(json.dumps({'delivery': [dict(r) for r in store.db.execute('SELECT * FROM delivery')],
                                  'progress': [dict(r) for r in store.db.execute(
                                      'SELECT book_id,state,COUNT(*) AS count FROM chapters GROUP BY book_id,state')],
                                  'agent_failures': [dict(r) for r in store.db.execute(
                                      "SELECT id,chapter,stage FROM calls WHERE provider=? AND status IN ('failed','reserved')",
                                      (cfg['agent']['provider'],))]}, ensure_ascii=False))
                return
            data, row = entry(store, cfg, args.book)
            if args.action == 'claim':
                if row['state'] != 'ready':
                    raise ValueError('Already claimed, sent or uncertain; never resend automatically')
                store.db.execute("UPDATE delivery SET state='sending' WHERE book_id=?", (args.book,))
                print(json.dumps(data, ensure_ascii=False))
            elif args.action == 'chunk':
                if row['state'] != 'sending' or not 0 <= args.offset < data['size'] or args.offset % 6144:
                    raise ValueError('Invalid chunk request')
                with Path(data['path']).open('rb') as f:
                    f.seek(args.offset)
                    # Multiple of three: encoded chunks concatenate without padding.
                    print(base64.urlsafe_b64encode(f.read(6144)).decode('ascii'))
            else:
                if row['state'] != 'sending':
                    raise ValueError('No in-flight send')
                if args.action == 'ack' and not args.message_id:
                    raise ValueError('Gmail message ID is required')
                store.db.execute('UPDATE delivery SET state=?,message_id=?,error=? WHERE book_id=?',
                                 ('submitted' if args.action == 'ack' else 'unknown',
                                  args.message_id or row['message_id'],
                                  None if args.action == 'ack' else 'Connector send outcome uncertain', args.book))
                print('Recorded')
        finally:
            store.close()


if __name__ == '__main__':
    main()
