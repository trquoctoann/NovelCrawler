from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata

from .agents import SCHEMA, output_schema


class BudgetStop(RuntimeError):
    pass


def reserve(store, cfg, book_id, chapter, stage, agent, prompt, now=None):
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    size = len(prompt.encode('utf-8'))
    if size > cfg['agent']['max_prompt_bytes']:
        raise BudgetStop('Prompt too large; split/review chapter manually')
    # Deliberately conservative byte-based reservation, NOT provider metering.
    tokens = size + cfg['agent']['max_output_tokens']
    limits = cfg['limits']
    db = store.db
    with store.transaction():
        # A failed account pauses that provider. The shared budget still includes
        # every provider, while an unavailable alternate cannot stop the active one.
        if db.execute("SELECT 1 FROM calls WHERE provider=? AND status IN ('reserved','failed') LIMIT 1",
                      (agent.provider,)).fetchone():
            raise BudgetStop('Unresolved agent call: inspect and acknowledge before resuming')
        count = db.execute('SELECT COUNT(*) FROM calls WHERE book_id=? AND chapter=? AND stage=?',
                           (book_id, chapter, stage)).fetchone()[0]
        if count >= limits['max_attempts_per_chapter_stage']:
            raise BudgetStop('Chapter stage exhausted its lifetime attempts')
        for where, args, calls_key, tokens_key in [
            ('WHERE day=?', (day,), 'calls_per_day', 'reserved_tokens_per_day'),
            ('', (), 'calls_total', 'reserved_tokens_total'),
        ]:
            calls, spent = db.execute(f'SELECT COUNT(*),COALESCE(SUM(reserved),0) FROM calls {where}', args).fetchone()
            if calls + 1 > limits[calls_key] or spent + tokens > limits[tokens_key]:
                raise BudgetStop('Budget ceiling reached')
        last = db.execute('SELECT MAX(created) FROM calls').fetchone()[0]
        if last is not None and now - last < limits['min_call_interval_seconds']:
            raise BudgetStop('Minimum agent interval not elapsed')
        cur = db.execute('INSERT INTO calls(book_id,chapter,stage,provider,model,created,day,reserved,prompt_hash) '
                         'VALUES(?,?,?,?,?,?,?,?,?)',
                         (book_id, chapter, stage, agent.provider, agent.model, now, day, tokens,
                          hashlib.sha256(prompt.encode()).hexdigest()))
        return cur.lastrowid


def validate_result(raw, source, glossary, allow_issues=False):
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != set(SCHEMA['required']):
        raise ValueError('Unexpected result schema')
    if (not isinstance(data['issues'], list) or any(not isinstance(i, str) for i in data['issues'])
            or (data['issues'] and not allow_issues)):
        raise ValueError('Agent reported issues: ' + str(data['issues'])[:400])
    for key, maximum in [('title', 300), ('continuity', 2500)]:
        if not isinstance(data[key], str) or not data[key].strip() or len(data[key]) > maximum:
            raise ValueError(f'Invalid {key}')
    paragraphs = data['paragraphs']
    if not isinstance(paragraphs, list) or len(paragraphs) != len(source):
        raise ValueError('Paragraph coverage mismatch')
    for number, (paragraph, original) in enumerate(zip(paragraphs, source), 1):
        if (not isinstance(paragraph, dict) or set(paragraph) != {'id', 'text'}
                or type(paragraph['id']) is not int or paragraph['id'] != number
                or not isinstance(paragraph['text'], str)):
            raise ValueError('Missing, duplicate or unordered paragraph ID')
        text = unicodedata.normalize('NFC', paragraph['text']).strip()
        if not text or '\n' in text or re.search(r'[\u4e00-\u9fff\ue000-\uf8ff]|<[^>]*>|```', text):
            raise ValueError('Empty/untranslated paragraph or unwanted markup')
        if len(original) > 20 and not .35 <= len(text) / len(original) <= 12:
            raise ValueError('Suspicious paragraph length')
        # Exact glossary detection is a review gate; legitimate variants need glossary review.
        for zh, vi in glossary.items():
            if zh in original and vi not in text:
                raise ValueError(f'Glossary mismatch: {zh} -> {vi}')
        if re.findall(r'\d+(?:[.,]\d+)*', original) != re.findall(r'\d+(?:[.,]\d+)*', text):
            raise ValueError('Numeric literals changed; review required')
        paragraph['text'] = text
    if re.search(r'[\u4e00-\u9fff]|<[^>]*>|```|[\r\n]', data['title']):
        raise ValueError('Title is untranslated or contains markup')
    return data


class Translator:
    def __init__(self, store, config, agent):
        self.store, self.cfg, self.agent = store, config, agent

    def run(self, book, stage, batch):
        if not self.cfg['agent']['enabled']:
            return 0
        if stage not in ('translate', 'edit'):
            raise ValueError('Invalid translation stage')
        if hasattr(self.agent, 'preflight'):
            self.agent.preflight()
        glossary = json.loads(book['glossary'].read_text(encoding='utf-8'))
        if not isinstance(glossary, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                or not k or not v for k, v in glossary.items()):
            raise ValueError('Glossary must map nonempty strings to strings')
        digest = hashlib.sha256(json.dumps(glossary, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        db = self.store.db
        old = db.execute('SELECT glossary_hash FROM books WHERE id=?', (book['id'],)).fetchone()[0]
        if old and old != digest:
            raise ValueError('Glossary changed mid-book; reconcile prior translations before continuing')
        db.execute('UPDATE books SET glossary_hash=? WHERE id=?', (digest, book['id']))
        field, required = ('translated', 'crawled') if stage == 'translate' else ('edited', 'translated')
        done = 0
        for _ in range(batch):
            ch = db.execute(f'SELECT * FROM chapters WHERE book_id=? AND {field} IS NULL ORDER BY number LIMIT 1',
                            (book['id'],)).fetchone()
            if ch is None or ch['state'] != required:
                break
            source = json.loads(ch['source'])
            prior = db.execute('SELECT * FROM chapters WHERE book_id=? AND number=?',
                               (book['id'], ch['number'] - 1)).fetchone()
            context = None
            if prior:
                previous = json.loads(prior[field]) if prior[field] else None
                if not previous:
                    break
                context = {'continuity': previous['continuity'], 'excerpt': previous['paragraphs'][-5:]}
            prompts = Path(__file__).parent / 'prompts'
            rules = (prompts / 'translation' / 'SKILL.md').read_text(encoding='utf-8')
            if stage == 'edit':
                rules += '\n' + (prompts / 'editing.md').read_text(encoding='utf-8')
            payload = {'book': book['title'], 'chapter': ch['number'], 'title': ch['title'],
                       'expected_paragraph_count': len(source),
                       'glossary': glossary, 'previous_context': context,
                       'source': [{'id': i, 'text': p} for i, p in enumerate(source, 1)],
                       'draft': json.loads(ch['translated']) if stage == 'edit' else None}
            prompt = rules + '\nOUTPUT SCHEMA:\n' + json.dumps(output_schema(len(source))) + '\nDATA:\n' + json.dumps(payload, ensure_ascii=False)
            last = db.execute('SELECT MAX(created) FROM calls').fetchone()[0]
            if last is not None:
                wait = self.cfg['limits']['min_call_interval_seconds'] - (time.time() - last)
                if wait > 0:
                    time.sleep(wait)
            call_id = reserve(self.store, self.cfg, book['id'], ch['number'], stage, self.agent, prompt)
            db.execute('UPDATE chapters SET state=? WHERE book_id=? AND number=?',
                       (stage + '_inflight', book['id'], ch['number']))
            raw = None
            try:
                raw, usage = self.agent.generate(prompt)
                # Persist recoverable response before validation; never refund unknown usage.
                db.execute('UPDATE calls SET response=?,usage=?,reserved=MAX(reserved,?) WHERE id=?',
                           (raw, usage, usage or 0, call_id))
                result = validate_result(raw, source, glossary, allow_issues=stage == 'translate')
                with self.store.transaction():
                    db.execute(f'UPDATE chapters SET {field}=?,summary=?,state=?,error=NULL WHERE book_id=? AND number=?',
                               (json.dumps(result, ensure_ascii=False), result['continuity'],
                                'translated' if stage == 'translate' else 'edited', book['id'], ch['number']))
                    db.execute("UPDATE calls SET status='succeeded' WHERE id=?", (call_id,))
                done += 1
            except Exception as exc:
                with self.store.transaction():
                    db.execute("UPDATE calls SET status='failed' WHERE id=?", (call_id,))
                    db.execute('UPDATE chapters SET state=?,error=? WHERE book_id=? AND number=?',
                               (stage + '_failed', str(exc)[:500], book['id'], ch['number']))
                raise
        return done


def retry(store, book_id, number):
    """Explicit operator acknowledgement; historical reservations always retained."""
    with store.transaction():
        ch = store.db.execute('SELECT * FROM chapters WHERE book_id=? AND number=?', (book_id, number)).fetchone()
        if not ch:
            raise ValueError('Unknown chapter')
        states = {'crawl_failed': 'discovered', 'translate_failed': 'crawled',
                  'translate_inflight': 'crawled', 'edit_failed': 'translated', 'edit_inflight': 'translated'}
        reserved = store.db.execute("SELECT 1 FROM calls WHERE book_id=? AND chapter=? AND status='reserved'",
                                    (book_id, number)).fetchone()
        if ch['state'] not in states and not reserved:
            raise ValueError('Chapter does not need retry')
        state = states.get(ch['state'], ch['state'])
        store.db.execute('UPDATE chapters SET state=?,error=NULL WHERE book_id=? AND number=?', (state, book_id, number))
        store.db.execute("UPDATE calls SET status='acknowledged' WHERE book_id=? AND chapter=? AND status IN ('failed','reserved')",
                         (book_id, number))
