import hashlib
import json
import logging
import time

import httpx

from .alignment import align, body_fingerprint, catalog, extract_chapter, overlap
from .crawler import Crawler, parse_toc


class MultiSourceCrawler(Crawler):
    def review(self, book_id, number, reason):
        self.store.db.execute('INSERT INTO source_reviews(book_id,number,reason) VALUES(?,?,?) '
                              'ON CONFLICT(book_id,number) DO UPDATE SET reason=excluded.reason',
                              (book_id, number, reason))

    def discover(self, book):
        db = self.store.db
        if not db.execute('SELECT toc_ready FROM books WHERE id=?', (book['id'],)).fetchone()[0]:
            html = self.fetch(book['source_url'], book['encoding'])
            catalog(html, book, book) # Verify work identity before fixing canonical IDs.
            canonical = parse_toc(html, book)
            with self.store.transaction():
                for entry in canonical:
                    db.execute('INSERT OR IGNORE INTO chapters(book_id,number,url,title) VALUES(?,?,?,?)',
                               (book['id'], entry['number'], entry['url'], entry['title']))
                db.execute('UPDATE books SET toc_ready=1 WHERE id=?', (book['id'],))
        canonical = [dict(r) for r in db.execute('SELECT number,title FROM chapters WHERE book_id=? ORDER BY number', (book['id'],))]
        for source in book['sources']:
            signature = hashlib.sha256(json.dumps({'source': source, 'user_agent': self.cfg['user_agent']}, sort_keys=True).encode()).hexdigest()
            old = db.execute('SELECT * FROM source_scans WHERE book_id=? AND source_id=?', (book['id'], source['id'])).fetchone()
            if old and old['signature'] == signature:
                if old['state'] == 'ready' or old['attempts'] >= 3 or old['retry_at'] > time.time():
                    continue
            attempts = old['attempts'] + 1 if old and old['signature'] == signature else 1
            db.execute('INSERT OR REPLACE INTO source_scans VALUES(?,?,?,?,?,?,?,?)',
                       (book['id'], source['id'], signature, 'fetching', attempts,
                        time.time() + 300 * 2 ** (attempts - 1), None, 0))
            try:
                entries = catalog(self.fetch(source['source_url'], source['encoding']), source, book)
                matching_canonical = ([dict(number=e['number'], title=e['original_title'])
                                       for e in book['_reference_entries']]
                                      if source.get('reference_titles') else canonical)
                mapping = align(matching_canonical, entries, literal=book.get('literal_titles', False))
                with self.store.transaction():
                    db.execute('DELETE FROM source_candidates WHERE book_id=? AND source_id=?', (book['id'], source['id']))
                    for i, e in enumerate(entries):
                        number, proof = mapping.get(i, (None, 'ambiguous-or-unmatched-title'))
                        db.execute('INSERT INTO source_candidates(book_id,source_id,url,source_number,title,canonical_number,proof) '
                                   'VALUES(?,?,?,?,?,?,?)', (book['id'], source['id'], e['url'], e['number'], e['title'], number, proof))
                    db.execute('INSERT OR REPLACE INTO source_scans VALUES(?,?,?,?,?,?,?,?)',
                               (book['id'], source['id'], signature, 'ready', attempts, 0, None, len(mapping)))
                    db.execute("UPDATE chapters SET state='discovered' WHERE book_id=? AND source IS NULL AND state IN ('source_review','source_waiting')",
                               (book['id'],))
                logging.info('%s: mapped %s/%s chapters from %s', book['id'], len(mapping), len(canonical), source['id'])
            except InterruptedError:
                raise
            except Exception as exc:
                db.execute('INSERT OR REPLACE INTO source_scans VALUES(?,?,?,?,?,?,?,?)',
                           (book['id'], source['id'], signature, 'failed', attempts,
                            time.time() + 300 * 2 ** (attempts - 1), str(exc)[:500], 0))
                logging.warning('%s source %s: %s', book['id'], source['id'], exc)

    def run(self, book, batch):
        db = self.store.db
        self.discover(book)
        sources = {s['id']: s for s in book['sources']}
        priority = {s['id']: i for i, s in enumerate(book['sources'])}
        # Cache prior fingerprints once per batch, preserving punctuation-insensitive
        # checks for databases created by the single-source version too.
        existing = [(r['number'], body_fingerprint(json.loads(r['source']))[0]) for r in
                    db.execute('SELECT number,source FROM chapters WHERE book_id=? AND source IS NOT NULL', (book['id'],))]
        catalog_waiting = any(not (scan := db.execute(
            'SELECT state,attempts FROM source_scans WHERE book_id=? AND source_id=?',
            (book['id'], source_id)).fetchone()) or
            (scan['state'] != 'ready' and scan['attempts'] < 3) for source_id in sources)
        by_chapter = {}
        for candidate in db.execute('SELECT * FROM source_candidates WHERE book_id=?', (book['id'],)):
            if candidate['source_id'] in sources:
                by_chapter.setdefault(candidate['canonical_number'], []).append(candidate)
        queued = db.execute("SELECT * FROM chapters WHERE book_id=? AND source IS NULL AND state IN "
                          "('discovered','source_waiting','crawl_failed') "
                          "ORDER BY CASE WHEN EXISTS "
                          "(SELECT 1 FROM source_candidates sc WHERE sc.book_id=chapters.book_id "
                          "AND sc.canonical_number=chapters.number AND sc.source_id=?) THEN 1 ELSE 0 END, number",
                          (book['id'], book['sources'][0]['id'])).fetchall()
        rows = []
        for row in queued:
            pending = [c for c in by_chapter.get(row['number'], []) if c['state'] == 'pending' and c['attempts'] < 3]
            if (row['state'] == 'source_waiting' and not any(c['retry_at'] <= time.time() for c in pending)
                    and (catalog_waiting or pending)):
                continue # A waiting gap must not starve later available chapters.
            rows.append(row)
            if len(rows) >= batch:
                break
        done = 0
        for ch in rows:
            if getattr(self, 'stop', None) is not None and self.stop.is_set():
                break
            candidates = by_chapter.get(ch['number'], [])
            candidates.sort(key=lambda r: priority[r['source_id']])
            reasons = []
            # An unavailable catalog has not yet exhausted the configured sources.
            waiting = catalog_waiting
            accepted = False
            for candidate in candidates:
                if candidate['state'] == 'rejected' or candidate['attempts'] >= 3:
                    reasons.append(f"{candidate['source_id']}: {candidate['error'] or 'attempt limit'}")
                    continue
                if candidate['retry_at'] > time.time():
                    waiting = True
                    continue
                source = sources[candidate['source_id']]
                attempts = candidate['attempts'] + 1
                # Reserve the HTTP attempt before sending; crash does not reset its cap.
                db.execute('UPDATE source_candidates SET attempts=?,retry_at=? WHERE id=?',
                           (attempts, time.time() + 300 * 2 ** (attempts - 1), candidate['id']))
                try:
                    html = self.fetch(candidate['url'], source['encoding'])
                    paragraphs = extract_chapter(html, source, candidate['title'], self.cfg)
                    reference = book.get('_reference_entries')
                    if reference:
                        expected = reference[ch['number'] - 1]['word_count']
                        actual = len(''.join(paragraphs))
                        if not expected * .95 <= actual <= expected * 1.10:
                            raise ValueError(f'Body length {actual} differs from reference {expected}; review edition/truncation')
                    text, digest = body_fingerprint(paragraphs)
                    duplicate = next((n for n, old in existing if overlap(text, old) >= .85), None)
                    if duplicate is not None:
                        raise ValueError(f'Body duplicates/overlaps accepted chapter {duplicate}')
                    with self.store.transaction():
                        db.execute("UPDATE chapters SET source=?,source_hash=?,state='crawled',error=NULL WHERE book_id=? AND number=? AND source IS NULL",
                                   (json.dumps(paragraphs, ensure_ascii=False), digest, book['id'], ch['number']))
                        db.execute('INSERT INTO source_provenance VALUES(?,?,?,?,?,?,?,?)',
                                   (book['id'], ch['number'], source['id'], candidate['url'], candidate['source_number'],
                                    candidate['title'], candidate['proof'] + '+page-title+length+dedup', digest))
                        db.execute("UPDATE source_candidates SET state='accepted',error=NULL WHERE id=?", (candidate['id'],))
                        db.execute('DELETE FROM source_reviews WHERE book_id=? AND number=?', (book['id'], ch['number']))
                    existing.append((ch['number'], text))
                    done += 1
                    accepted = True
                    break
                except InterruptedError:
                    raise
                except Exception as exc:
                    transient = isinstance(exc, (httpx.TransportError, TimeoutError)) or (
                        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (408, 429, 500, 502, 503, 504))
                    state = 'pending' if transient and attempts < 3 else 'rejected'
                    waiting |= state == 'pending'
                    db.execute('UPDATE source_candidates SET state=?,error=? WHERE id=?', (state, str(exc)[:500], candidate['id']))
                    reasons.append(f"{source['id']}: {exc}")
            if not accepted:
                reason = '; '.join(reasons)[:1500] or 'No unambiguous matching chapter from available catalogs'
                state = 'source_waiting' if waiting else 'source_review'
                db.execute('UPDATE chapters SET state=?,error=? WHERE book_id=? AND number=?',
                           (state, reason, book['id'], ch['number']))
                self.review(book['id'], ch['number'], reason)
        return done
