from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import time
import unicodedata

from .agents import SCHEMA, output_schema, MODELS
from .numbers import numeric_literals_match
from .glossary import (mismatches, requirements, equivalent, matches, normalize_aliases,
                       source_mentions, ordinary_reading_allowed, contains)
from .style import apply_style_revision
from .response_json import loads as load_response
from .agent_errors import AgentResponseError, ContentPolicyError, policy_message


class BudgetStop(RuntimeError):
    pass


class EditingReviewError(ValueError):
    def __init__(self, result, findings):
        self.result, self.findings = result, findings
        super().__init__('; '.join(findings))


class GlossaryReviewError(EditingReviewError):
    pass


def source_input_hash(store, book_id, chapter):
    row = store.db.execute('SELECT source FROM chapters WHERE book_id=? AND number=?', (book_id, chapter)).fetchone()
    return hashlib.sha256(row['source'].encode()).hexdigest() if row and row['source'] else None


def reserve(store, cfg, book_id, chapter, stage, agent, prompt, now=None, parent_call_id=None):
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    size = len(prompt.encode('utf-8'))
    if size > cfg['agent']['max_prompt_bytes']:
        raise BudgetStop('Prompt too large; split/review chapter manually')
    # Deliberately conservative byte-based reservation, NOT provider metering.
    tokens = size + cfg['agent']['max_output_tokens']
    limits = cfg['limits']
    db = store.db
    source_hash = source_input_hash(store, book_id, chapter)
    with store.transaction():
        pause = db.execute('SELECT retry_at,reason FROM provider_pauses WHERE provider=?',
                           (agent.provider,)).fetchone()
        if pause and pause['retry_at'] > now:
            raise BudgetStop(f"Provider paused until {pause['retry_at']}: {pause['reason']}")
        # Account/transport failures pause the provider. A returned draft rejected
        # by validation blocks its chapter, while earlier ready edits may continue.
        if db.execute("SELECT 1 FROM calls WHERE provider=? AND stage!='glossary_review' AND (status='reserved' OR "
                      "(status='failed' AND COALESCE(failure_kind,'') NOT IN ('internal','content_blocked') AND NOT "
                      "(COALESCE(failure_kind,'') IN ('review','glossary','quality') AND response IS NOT NULL))) LIMIT 1",
                      (agent.provider,)).fetchone():
            raise BudgetStop('Unresolved agent call: inspect and acknowledge before resuming')
        count = db.execute("SELECT COUNT(*) FROM calls WHERE book_id=? AND chapter=? AND stage=? "
                           "AND provider=? AND model=? "
                           "AND style_revision=? AND (source_input_hash=? OR source_input_hash IS NULL) "
                           "AND COALESCE(failure_kind,'') NOT IN ('quota','abandoned_test','rolled_back')",
                           (book_id, chapter, stage, agent.provider, agent.model,
                            cfg['agent'].get('style_revision', 'legacy'), source_hash)).fetchone()[0]
        if count >= limits['max_attempts_per_chapter_stage']:
            raise BudgetStop('Chapter stage exhausted its lifetime attempts')
        for where, args, calls_key, tokens_key in [
            ('WHERE day=?', (day,), 'calls_per_day', 'reserved_tokens_per_day'),
            ('', (), 'calls_total', 'reserved_tokens_total'),
        ]:
            calls, spent = db.execute(f'SELECT COUNT(*),COALESCE(SUM(reserved),0) FROM calls {where}', args).fetchone()
            if ((limits[calls_key] and calls + 1 > limits[calls_key]) or
                    (limits[tokens_key] and spent + tokens > limits[tokens_key])):
                raise BudgetStop('Budget ceiling reached')
        last = db.execute('SELECT MAX(created) FROM calls').fetchone()[0]
        if last is not None and now - last < limits['min_call_interval_seconds']:
            raise BudgetStop('Minimum agent interval not elapsed')
        cur = db.execute('INSERT INTO calls(book_id,chapter,stage,provider,model,created,day,reserved,prompt_hash,style_revision,source_input_hash) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         (book_id, chapter, stage, agent.provider, agent.model, now, day, tokens,
                          hashlib.sha256(prompt.encode()).hexdigest(), cfg['agent'].get('style_revision', 'legacy'), source_hash))
        call_id = cur.lastrowid
        if parent_call_id is not None:
            db.execute('INSERT INTO glossary_reviews VALUES(?,?,?)', (parent_call_id, agent.provider, call_id))
        return call_id


def load_rendering_aliases(book):
    path = book.get('rendering_aliases')
    if not path:
        return {}
    aliases = json.loads(Path(path).read_text(encoding='utf-8'))
    if (not isinstance(aliases, dict) or any(
            not isinstance(zh, str) or not zh or not isinstance(values, list) or
            any(not isinstance(value, str) or not value.strip() for value in values)
            for zh, values in aliases.items())):
        raise ValueError('Rendering aliases must map source terms to lists of verified Vietnamese variants')
    return aliases


def valid_contextual_readings(readings, source, glossary):
    """Optional model annotations grant a waiver only with complete valid evidence.

    Bad annotations are discarded, leaving the normal entity checks in place.
    The original response remains in the call ledger for diagnosis/recovery.
    """
    if not isinstance(readings, list) or len(readings) > 100:
        return []
    accepted, seen = [], set()
    fields = {'paragraph_id', 'source', 'source_start', 'source_quote', 'translation', 'reason'}
    for reading in readings:
        if not isinstance(reading, dict) or set(reading) != fields:
            continue
        pid, start, zh = reading['paragraph_id'], reading['source_start'], reading['source']
        if (type(pid) is not int or not 1 <= pid <= len(source) or type(start) is not int or start < 0
                or not isinstance(zh, str) or zh not in glossary
                or reading['source_quote'] != source[pid - 1]
                or not isinstance(reading['translation'], str) or not 2 <= len(reading['translation']) <= 500
                or not isinstance(reading['reason'], str) or not 15 <= len(reading['reason']) <= 1000):
            continue
        mention = next((m for m in source_mentions(source[pid - 1], glossary)
                        if m.start() == start and m.group() == zh), None)
        if mention is None or not ordinary_reading_allowed(source[pid - 1], mention) or (pid, start) in seen:
            continue
        seen.add((pid, start))
        accepted.append(reading)
    return accepted


def validate_result(raw, source, glossary, allow_issues=False, rendering_aliases=None, reviewed_readings=()):
    rendering_aliases = rendering_aliases or {}
    data = load_response(raw)
    if not isinstance(data, dict) or set(data) - {'terms', 'source_notes', 'glossary_readings'} != set(SCHEMA['required']):
        raise ValueError('Unexpected result schema')
    if not isinstance(data['issues'], list) or any(not isinstance(i, str) for i in data['issues']):
        raise ValueError('Invalid issues list')
    refused = [issue for issue in data['issues'] if issue.lstrip().startswith('POLICY:')]
    if refused:
        raise ContentPolicyError('; '.join(refused))

    data['issues'] = [i for i in data['issues'] if not i.lstrip().startswith('SOURCE:')]
    quality_findings = (['Agent reported issues: ' + json.dumps(data['issues'], ensure_ascii=False)]
                        if data['issues'] and not allow_issues else [])
    def quality_issue(message):
        if not allow_issues:
            quality_findings.append(message)
        elif message not in data['issues']:
            data['issues'].append(message)
    glossary_findings = []
    for key, maximum in [('title', 300), ('continuity', 2500)]:
        if not isinstance(data[key], str) or not data[key].strip() or len(data[key]) > maximum:
            raise ValueError(f'Invalid {key}')
    paragraphs = data['paragraphs']
    if not isinstance(paragraphs, list) or len(paragraphs) != len(source):
        raise ValueError('Paragraph coverage mismatch')
    readings = valid_contextual_readings(data.get('glossary_readings', []), source, glossary)
    if 'glossary_readings' in data:
        data['glossary_readings'] = readings
    ordinary = {}
    # Separate contextual review can resolve short homonyms that dictionary
    # segmentation cannot recognize. These decisions come from the audit ledger,
    # never from an extra field supplied by the translation model.
    from .glossary import PLACE_TYPES
    for reading in reviewed_readings:
        pid, start, zh = reading['paragraph_id'], reading['source_start'], reading['source']
        if (type(pid) is not int or not 1 <= pid <= len(source) or type(start) is not int or start < 0
                or len(zh) != 2 or zh[-1] in PLACE_TYPES or zh.endswith('家')
                or glossary.get(zh) != reading['canonical'] or reading['source_quote'] != source[pid - 1]
                or not isinstance(paragraphs[pid - 1], dict)
                or not isinstance(paragraphs[pid - 1].get('text'), str)
                or not reading['translation_quote'].strip()
                or not contains(paragraphs[pid - 1]['text'], reading['translation_quote'])):
            continue
        if any(m.start() == start and m.group() == zh for m in source_mentions(source[pid - 1], glossary)):
            ordinary.setdefault(pid, set()).add(start)
    for reading in readings:
        pid, start = reading['paragraph_id'], reading['source_start']
        ordinary.setdefault(pid, set()).add(start)
    # Source commentary is advisory metadata, never a publication gate.
    # Raw responses preserve all original notes for audit; EPUB omits them.
    notes = data.get('source_notes', [])
    data['source_notes'] = ([n for n in notes[:20] if isinstance(n, dict)
                             and isinstance(n.get('note'), str)] if isinstance(notes, list) else [])
    for number, (paragraph, original) in enumerate(zip(paragraphs, source), 1):
        if (not isinstance(paragraph, dict) or set(paragraph) - {'join_previous'} != {'id', 'text'}
                or type(paragraph['id']) is not int or paragraph['id'] != number
                or not isinstance(paragraph['text'], str)):
            raise ValueError('Missing, duplicate or unordered paragraph ID')
        if 'join_previous' in paragraph and type(paragraph['join_previous']) is not bool:
            raise ValueError('Invalid paragraph layout flag')
        if number == 1 and paragraph.get('join_previous'):
            raise ValueError('First paragraph cannot join a preceding paragraph')
        from .reading import normalize_text
        text = normalize_text(paragraph['text'])
        # Source-grounded ordinary readings allow idiomatic paraphrases. The
        # evidence/identity checks above remain mandatory; an exact Vietnamese
        # substring in optional commentary is not a fidelity requirement.
        if not text or re.search(r'[\ue000-\uf8ff]|<[^>]*>|```', text):
            raise ValueError(f'Paragraph {number}: empty text or unwanted markup')
        if re.search(r'[\u4e00-\u9fff]', text):
            quality_issue(f'Paragraph {number}: untranslated Chinese remains; translate from the supplied source')
        if len(original) > 20 and not .35 <= len(text) / len(original) <= 12:
            raise ValueError('Suspicious paragraph length')
        paragraph['text'] = text
    from .reading import alignment_groups
    aligned = []
    for indices in alignment_groups(source, paragraphs):
        original = ''.join(source[i] for i in indices)
        candidates, offset = set(), 0
        for i in indices:
            candidates.update((offset + mention.start(), mention.group())
                              for mention in source_mentions(source[i], glossary)
                              if mention.start() in ordinary.get(i + 1, ()))
            offset += len(source[i])
            paragraphs[i]['text'] = normalize_aliases(original, paragraphs[i]['text'], glossary, rendering_aliases)
        waived = {mention.start() for mention in source_mentions(original, glossary)
                  if (mention.start(), mention.group()) in candidates}
        text = ' '.join(paragraphs[i]['text'] for i in indices)
        aligned.append((original, text, waived))
        label = ('Paragraph ' + str(indices[0] + 1) if len(indices) == 1 else
                 'Joined paragraphs ' + ','.join(str(i + 1) for i in indices))
        for mismatch in mismatches(original, text, glossary, waived):
            message = f'{label}: glossary mismatch: {mismatch}; preserve the named entity'
            if allow_issues:
                quality_issue(message)
            else:
                glossary_findings.append(message)
        if not numeric_literals_match(original, text):
            quality_issue(f'{label}: numeric literals changed; correct against the supplied source')
    if re.search(r'[\u4e00-\u9fff]|<[^>]*>|```|[\r\n]', data['title']):
        raise ValueError('Title is untranslated or contains markup')
    terms = data.get('terms', [])
    if not isinstance(terms, list) or len(terms) > 100:
        raise ValueError('Invalid term registry additions')
    seen = {}
    supported_terms = []
    for term in terms:
        if not isinstance(term, dict) or set(term) - {'kind'} != {'source', 'translation'}:
            raise ValueError('Invalid term entry')
        if term.get('kind', 'entity') not in ('entity', 'preferred'):
            raise ValueError('Invalid term kind')
        zh, vi = term['source'], term['translation']
        if (not isinstance(zh, str) or not isinstance(vi, str) or not 1 <= len(zh) <= 50 or
                not 1 <= len(vi.strip()) <= 150 or not re.search(r'[\u4e00-\u9fff]', zh) or
                re.search(r'[\u4e00-\u9fff\r\n<>]', vi)):
            raise ValueError('Invalid Hán–Việt term')
        if zh in glossary and vi.casefold() in {v.casefold() for v in rendering_aliases.get(zh, [])}:
            vi = term['translation'] = glossary[zh]
        if zh in glossary and equivalent(zh, glossary[zh], vi):
            vi = term['translation'] = glossary[zh]
        if ((zh in glossary and glossary[zh].casefold() != vi.casefold()) or
                (zh in seen and seen[zh].casefold() != vi.casefold())):
            # Optional proposals never overwrite existing identity or invalidate
            # correct prose. Raw proposals remain in the durable call response.
            continue
        if not any(matches(zh, vi, text, original, mention.end())
                   for original, text, waived in aligned
                   for mention in source_mentions(original, {**glossary, zh: vi})
                   if mention.group() == zh and mention.start() not in waived):
            # Proposed registry additions are optional metadata. Do not lock an
            # unsupported paraphrase, or discard otherwise valid chapter prose.
            # The original proposal remains in calls.response for auditing.
            continue
        seen[zh] = vi
        supported_terms.append(term)
    if 'terms' in data:
        data['terms'] = supported_terms
    if quality_findings:
        raise EditingReviewError(data, glossary_findings + quality_findings)
    if glossary_findings:
        raise GlossaryReviewError(data, glossary_findings)
    return data


def relevant_glossary(store, book_id, glossary, source, chapter=None):
    combined = dict(glossary)
    # Website line breaks can split a name itself (武 / 穆). Include these
    # candidates in prompt guidance; validation only combines explicit joins
    # across unfinished source sentences, never arbitrary chapter-wide text.
    unwrapped_source = source.replace('\n', '')
    for row in store.db.execute("SELECT source,translation FROM book_terms WHERE book_id=? AND kind='entity' AND chapter<=?",
                               (book_id, chapter if chapter is not None else 2147483647)):
        if len(row['source']) > 1 and row['source'] in unwrapped_source:
            if row['source'] in combined and combined[row['source']] != row['translation']:
                raise ValueError('Static glossary conflicts with persistent terms')
            combined[row['source']] = row['translation']
    active = {mention.group() for text in (source, unwrapped_source) for mention in source_mentions(text, combined)}
    return {zh: vi for zh, vi in combined.items() if zh in active}


def save_terms(store, book_id, chapter, result, approved=True):
    for term in result.get('terms', []):
        old = store.db.execute('SELECT translation,kind FROM book_terms WHERE book_id=? AND source=?',
                               (book_id, term['source'])).fetchone()
        kind = term.get('kind', 'entity')
        if len(term['source']) == 1:
            kind = 'preferred' # An isolated surname must not match 胡说/周围/etc.
        if not approved and kind == 'entity':
            kind = 'candidate'
        if old and old['kind'] == 'candidate' and approved:
            store.db.execute('UPDATE book_terms SET translation=?,kind=?,chapter=? WHERE book_id=? AND source=?',
                             (term['translation'], kind, chapter, book_id, term['source']))
            continue
        if old and old['kind'] in ('preferred', 'candidate'):
            continue # Preserve the contextual preference without enforcing one phrasing.
        if old:
            continue # Immutable approved identity; proposals are not updates.
        store.db.execute('INSERT OR IGNORE INTO book_terms(book_id,source,translation,chapter,kind) VALUES(?,?,?,?,?)',
                         (book_id, term['source'], term['translation'], chapter, kind))


def editing_draft(raw, source, glossary, aliases, reviewed_readings=()):
    """Recompute machine findings; never ask an editor to fix an obsolete false alarm."""
    draft = load_response(raw)
    draft['issues'] = [issue for issue in draft['issues'] if not re.match(
        r'^((?:Paragraph \d+|Joined paragraphs \d+(?:,\d+)+): (glossary mismatch:|numeric literals changed;|untranslated Chinese remains;)|Term conflicts with locked glossary:)', issue)]
    return validate_result(json.dumps(draft, ensure_ascii=False), source, glossary,
                           allow_issues=True, rendering_aliases=aliases, reviewed_readings=reviewed_readings)


class Translator:
    def __init__(self, store, config, agent):
        self.store, self.cfg, self.agent = store, config, agent

    def run(self, book, stage, batch):
        if not self.cfg['agent']['enabled']:
            return 0
        if stage not in ('translate', 'edit'):
            raise ValueError('Invalid translation stage')
        apply_style_revision(self.store, self.cfg, book)
        glossary = json.loads(book['glossary'].read_text(encoding='utf-8'))
        rendering_aliases = load_rendering_aliases(book)
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
        while done < batch:
            if getattr(self, 'stop', None) is not None and self.stop.is_set():
                break
            ch = db.execute(f"SELECT * FROM chapters WHERE book_id=? AND {field} IS NULL AND state!='missing' ORDER BY number LIMIT 1",
                            (book['id'],)).fetchone()
            if ch is not None and ch['state'] == 'edit_failed' and stage == 'edit' and self.cfg['agent'].get('contextual_glossary_review'):
                parent = db.execute("SELECT * FROM calls WHERE book_id=? AND chapter=? AND stage='edit' AND response IS NOT NULL "
                    "AND status='failed' AND failure_kind IN ('glossary','review') AND source_input_hash=? AND style_revision=? ORDER BY id DESC LIMIT 1",
                    (book['id'], ch['number'], source_input_hash(self.store, book['id'], ch['number']),
                     self.cfg['agent'].get('style_revision', 'legacy'))).fetchone()
                if parent:
                    from .glossary_review import approved_readings, resolve
                    source = json.loads(ch['source'])
                    context = json.loads(parent['glossary_context']) if parent['glossary_context'] else relevant_glossary(
                        self.store, book['id'], glossary, '\n'.join(source), ch['number'])
                    try:
                        validate_result(parent['response'], source, context, rendering_aliases=rendering_aliases,
                                        reviewed_readings=approved_readings(self.store, book['id'], ch['number'], source))
                    except GlossaryReviewError as exc:
                        resolve(self.store, self.cfg, book, parent, source, context, exc.result,
                                rendering_aliases, getattr(self, 'stop', None))
                    recover(self.store, self.cfg)
                    if db.execute('SELECT edited FROM chapters WHERE book_id=? AND number=?', (book['id'], ch['number'])).fetchone()[0]:
                        done += 1
                        continue
            if ch is None or ch['state'] != required:
                break
            if hasattr(self.agent, 'preflight') and not getattr(self, '_preflight_done', False):
                self.agent.preflight()
                self._preflight_done = True
            source = json.loads(ch['source'])
            from .glossary_review import approved_readings
            reviewed = approved_readings(self.store, book['id'], ch['number'], source)
            chapter_glossary = relevant_glossary(self.store, book['id'], glossary, '\n'.join(source), ch['number'])
            preferred = {r['source']: r['translation'] for r in db.execute(
                "SELECT source,translation FROM book_terms WHERE book_id=? AND kind!='entity'", (book['id'],))
                if r['source'] in '\n'.join(source) and r['source'] not in chapter_glossary}
            prior = db.execute("SELECT * FROM chapters WHERE book_id=? AND number<? AND state!='missing' ORDER BY number DESC LIMIT 1",
                               (book['id'], ch['number'])).fetchone()
            gaps = [r[0] for r in db.execute("SELECT number FROM chapters WHERE book_id=? AND number<? AND number>? AND state='missing'",
                                            (book['id'], ch['number'], prior['number'] if prior else 0))]
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
                       'glossary': chapter_glossary, 'previous_context': context,
                       'preferred_terms': preferred,
                       'missing_previous_chapters': gaps,
                       'source': [{'id': i, 'text': p} for i, p in enumerate(source, 1)],
                       'draft': editing_draft(ch['translated'], source, chapter_glossary, rendering_aliases, reviewed) if stage == 'edit' else None}
            payload['paragraph_glossary'] = [
                {'id': i, 'terms': requirements(p, chapter_glossary)} for i, p in enumerate(source, 1)
                if requirements(p, chapter_glossary)]
            if stage == 'edit':
                rejected = db.execute("SELECT response,error FROM calls WHERE book_id=? AND chapter=? "
                    "AND stage='edit' AND style_revision=? AND (source_input_hash=? OR source_input_hash IS NULL) "
                    "AND failure_kind IN ('glossary','quality') AND response IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (book['id'], ch['number'], self.cfg['agent'].get('style_revision', 'legacy'),
                     source_input_hash(self.store, book['id'], ch['number']))).fetchone()
                if rejected:
                    payload['draft'] = load_response(rejected['response'])
                    payload['repair_findings'] = rejected['error']
            prompt = rules + '\nOUTPUT SCHEMA:\n' + json.dumps(output_schema(len(source))) + '\nDATA:\n' + json.dumps(payload, ensure_ascii=False)
            last = db.execute('SELECT MAX(created) FROM calls').fetchone()[0]
            if last is not None:
                wait = self.cfg['limits']['min_call_interval_seconds'] - (time.time() - last)
                if wait > 0:
                    if getattr(self, 'stop', None) is not None:
                        if self.stop.wait(wait):
                            break
                    else:
                        time.sleep(wait)
            call_id = reserve(self.store, self.cfg, book['id'], ch['number'], stage, self.agent, prompt)
            db.execute('UPDATE calls SET glossary_context=? WHERE id=?',
                       (json.dumps(chapter_glossary, ensure_ascii=False), call_id))
            db.execute('UPDATE chapters SET state=? WHERE book_id=? AND number=?',
                       (stage + '_inflight', book['id'], ch['number']))
            raw = None
            try:
                logging.info('%s chapter %s %s started: call=%s model=%s',
                             book['id'], ch['number'], stage, call_id, self.agent.model)
                db.execute('INSERT OR REPLACE INTO worker_progress VALUES(?,?,?,?)',
                           (book['id'], stage, time.time(), f'Chapter {ch["number"]} in progress; call={call_id}'))
                if self.agent.provider == 'agy':
                    self.agent.call_id = call_id
                raw, usage = self.agent.generate(prompt)
                # Persist recoverable response before validation; never refund unknown usage.
                db.execute('UPDATE calls SET response=?,usage=?,reserved=MAX(reserved,?) WHERE id=?',
                           (raw, usage, usage or 0, call_id))
                try:
                    result = validate_result(raw, source, chapter_glossary, allow_issues=stage == 'translate',
                                             rendering_aliases=rendering_aliases, reviewed_readings=reviewed)
                except GlossaryReviewError as exc:
                    if stage != 'edit' or not self.cfg['agent'].get('contextual_glossary_review'):
                        raise
                    db.execute("UPDATE calls SET status='failed',failure_kind='glossary',error=? WHERE id=?", (str(exc), call_id))
                    from .glossary_review import resolve
                    parent = db.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
                    result = resolve(self.store, self.cfg, book, parent, source, chapter_glossary,
                                     exc.result, rendering_aliases, getattr(self, 'stop', None))
                with self.store.transaction():
                    save_terms(self.store, book['id'], ch['number'], result, approved=stage == 'edit')
                    db.execute(f'UPDATE chapters SET {field}=?,summary=?,state=?,error=NULL WHERE book_id=? AND number=?',
                               (json.dumps(result, ensure_ascii=False), result['continuity'],
                                'translated' if stage == 'translate' else 'edited', book['id'], ch['number']))
                    db.execute("UPDATE calls SET status='succeeded',error=NULL,failure_kind=NULL,retry_at=0 WHERE id=?", (call_id,))
                    db.execute("UPDATE calls SET status='acknowledged' WHERE book_id=? AND chapter=? AND stage=? "
                               "AND status='failed' AND failure_kind IN ('glossary','quality','content_blocked')", (book['id'], ch['number'], stage))
                done += 1
                if stage == 'translate' and result['issues']:
                    logging.info('%s chapter %s draft queued for editing with %s review notes',
                                 book['id'], ch['number'], len(result['issues']))
                logging.info('%s chapter %s %s committed: call=%s', book['id'], ch['number'], stage, call_id)
            except Exception as exc:
                kind, delay = classify_failure(exc, self.cfg)
                with self.store.transaction():
                    usage = getattr(exc, 'usage', None)
                    if type(usage) is int and usage >= 0:
                        db.execute('UPDATE calls SET usage=?,reserved=MAX(reserved,?) WHERE id=?', (usage, usage, call_id))
                    db.execute("UPDATE calls SET status='failed',error=?,failure_kind=?,retry_at=? WHERE id=?",
                               (str(exc)[:1800], kind, time.time() + delay, call_id))
                    if kind == 'quota':
                        db.execute('INSERT OR REPLACE INTO provider_pauses VALUES(?,?,?)',
                                   (self.agent.provider, time.time() + delay, str(exc)[:500]))
                    db.execute('UPDATE chapters SET state=?,error=? WHERE book_id=? AND number=?',
                               (stage + '_failed', str(exc)[:500], book['id'], ch['number']))
                if isinstance(exc, EditingReviewError) and stage == 'edit' and queue_edit_repair(self.store, self.cfg, book['id'], ch['number']):
                    logging.info('%s chapter %s queued for automatic editing repair: %s', book['id'], ch['number'], exc)
                    continue
                raise
        return done


def classify_failure(exc, cfg):
    if isinstance(exc, ContentPolicyError) or (isinstance(exc, RuntimeError) and not isinstance(exc, BudgetStop)
                                               and policy_message(exc)):
        return 'content_blocked', 0
    if isinstance(exc, (KeyError, TypeError, AttributeError, AssertionError)):
        return 'internal', 0 # A completed local stage failure must not pause the provider's other stage.
    if isinstance(exc, GlossaryReviewError):
        return 'glossary', 0
    if isinstance(exc, EditingReviewError):
        return 'quality', 0
    message = str(exc).lower()
    if any(s in message for s in ('usage limit', 'rate limit', 'rate_limit', 'quota',
                                  'resource_exhausted', '429', 'out of tokens', 'insufficient credits')):
        return 'quota', cfg['agent'].get('quota_cooldown_seconds', 3600)
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(s in message for s in (
            'connection', 'timed out', 'temporarily', '503', '502', 'stream disconnected')):
        return 'transient', cfg['agent'].get('retry_cooldown_seconds', 60)
    return 'review', 0


def queue_edit_repair(store, cfg, book_id, number):
    attempts = store.db.execute("SELECT COUNT(*) FROM calls WHERE book_id=? AND chapter=? AND stage='edit' "
                               "AND provider=? AND model=? AND style_revision=? AND (source_input_hash=? OR source_input_hash IS NULL) "
                               "AND COALESCE(failure_kind,'') NOT IN ('quota','abandoned_test','rolled_back')",
                               (book_id, number, cfg['agent']['provider'], MODELS[cfg['agent']['provider']][0],
                                cfg['agent'].get('style_revision', 'legacy'), source_input_hash(store, book_id, number))).fetchone()[0]
    if attempts >= cfg['limits']['max_attempts_per_chapter_stage']:
        return False
    store.db.execute("UPDATE chapters SET state='translated' WHERE book_id=? AND number=?", (book_id, number))
    return True


def recover(store, cfg, now=None):
    """Call once under the process lock, before workers start. Keep every reservation.

    A durable valid response is committed without inference. An interrupted call or
    transient failure is eligible on a later manual run, within lifetime caps.
    Invalid output/authentication failures require review, never a retry loop.
    """
    now = time.time() if now is None else now
    books = {b['id']: b for b in cfg['books']}
    rows = store.db.execute("SELECT * FROM calls WHERE status IN ('reserved','failed') ORDER BY id").fetchall()
    for call in rows:
        book = books.get(call['book_id'])
        if not book or not book.get('enabled', True) or call['stage'] not in ('translate', 'edit'):
            continue
        if call['stage'] == 'edit' and call['style_revision'] != cfg['agent'].get('style_revision', 'legacy'):
            store.db.execute("UPDATE calls SET status='acknowledged' WHERE id=?", (call['id'],))
            continue # Old prose cannot satisfy the new style's final editing pass.
        ch = store.db.execute('SELECT * FROM chapters WHERE book_id=? AND number=?',
                              (call['book_id'], call['chapter'])).fetchone()
        if not ch or not ch['source']:
            continue
        if call['source_input_hash'] and call['source_input_hash'] != hashlib.sha256(ch['source'].encode()).hexdigest():
            store.db.execute("UPDATE calls SET status='acknowledged' WHERE id=?", (call['id'],))
            continue # A response for a superseded source cannot restore the current chapter.
        field = 'translated' if call['stage'] == 'translate' else 'edited'
        if ch[field]:
            store.db.execute("UPDATE calls SET status='acknowledged' WHERE id=?", (call['id'],))
            continue
        # Explicit refusals remain durable routing evidence. Never treat their
        # response as a valid draft, or retry the same provider after restart.
        if call['failure_kind'] == 'content_blocked':
            continue
        if call['provider'] == 'agy' and not call['response']:
            from .antigravity import recover_output
            try:
                saved = recover_output(cfg['agent'], call)
            except AgentResponseError as exc:
                kind, delay = classify_failure(exc, cfg)
                with store.transaction():
                    store.db.execute("UPDATE calls SET status='failed',failure_kind=?,error=?,retry_at=? WHERE id=?",
                                     (kind, str(exc)[:1800], now + delay, call['id']))
                    if exc.usage is not None:
                        store.db.execute('UPDATE calls SET usage=?,reserved=MAX(reserved,?) WHERE id=?',
                                         (exc.usage, exc.usage, call['id']))
                    store.db.execute('UPDATE chapters SET state=?,error=? WHERE book_id=? AND number=?',
                                     (call['stage'] + '_failed', str(exc)[:500], call['book_id'], call['chapter']))
                continue
            if saved:
                raw, usage = saved
                store.db.execute('UPDATE calls SET response=?,usage=?,reserved=MAX(reserved,?) WHERE id=?',
                                 (raw, usage, usage, call['id']))
                call = store.db.execute('SELECT * FROM calls WHERE id=?', (call['id'],)).fetchone()
        if call['response']:
            try:
                glossary = json.loads(book['glossary'].read_text(encoding='utf-8'))
                digest = hashlib.sha256(json.dumps(glossary, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                locked = store.db.execute('SELECT glossary_hash FROM books WHERE id=?', (book['id'],)).fetchone()[0]
                if locked and digest != locked:
                    raise ValueError('Glossary changed; cannot recover response')
                glossary = (json.loads(call['glossary_context']) if call['glossary_context'] else
                            relevant_glossary(store, book['id'], glossary, '\n'.join(json.loads(ch['source'])), ch['number']))
                response = call['response']
                reviewed = store.db.execute('SELECT * FROM reviewed_responses WHERE call_id=?', (call['id'],)).fetchone()
                if reviewed:
                    if (reviewed['original_hash'] != hashlib.sha256(response.encode()).hexdigest() or
                            reviewed['source_hash'] != hashlib.sha256(ch['source'].encode()).hexdigest()):
                        raise ValueError('Reviewed response no longer matches original source/output')
                    response = reviewed['response']
                from .glossary_review import approved_readings
                result = validate_result(response, json.loads(ch['source']), glossary,
                                         allow_issues=call['stage'] == 'translate',
                                         rendering_aliases=load_rendering_aliases(book),
                                         reviewed_readings=approved_readings(store, book['id'], ch['number'], json.loads(ch['source'])))
            except ContentPolicyError as exc:
                store.db.execute("UPDATE calls SET status='failed',failure_kind='content_blocked',error=? WHERE id=?",
                                 (str(exc)[:1800], call['id']))
                store.db.execute('UPDATE chapters SET state=?,error=? WHERE book_id=? AND number=?',
                                 (call['stage'] + '_failed', str(exc)[:500], call['book_id'], call['chapter']))
                continue
            except EditingReviewError as exc:
                kind, _ = classify_failure(exc, cfg)
                store.db.execute("UPDATE calls SET status='failed',failure_kind=?,error=? WHERE id=?", (kind, str(exc), call['id']))
                if call['stage'] == 'edit' and not (kind == 'glossary' and cfg['agent'].get('contextual_glossary_review')):
                    queue_edit_repair(store, cfg, call['book_id'], call['chapter'])
                continue
            except (ValueError, TypeError, KeyError):
                store.db.execute("UPDATE calls SET status='failed',failure_kind='review' WHERE id=?", (call['id'],))
                store.db.execute('UPDATE chapters SET state=? WHERE book_id=? AND number=?',
                                 (call['stage'] + '_failed', call['book_id'], call['chapter']))
                continue
            with store.transaction():
                save_terms(store, book['id'], ch['number'], result, approved=call['stage'] == 'edit')
                store.db.execute(f'UPDATE chapters SET {field}=?,summary=?,state=?,error=NULL WHERE book_id=? AND number=?',
                                 (json.dumps(result, ensure_ascii=False), result['continuity'],
                                  'translated' if field == 'translated' else 'edited', call['book_id'], call['chapter']))
                store.db.execute("UPDATE calls SET status='succeeded',error=NULL,failure_kind=NULL,retry_at=0 WHERE id=?", (call['id'],))
                store.db.execute("UPDATE calls SET status='acknowledged' WHERE book_id=? AND chapter=? AND stage=? "
                                 "AND status='failed' AND id!=?", (book['id'], ch['number'], call['stage'], call['id']))
                store.db.execute('INSERT OR REPLACE INTO worker_progress VALUES(?,?,?,?)',
                                 (book['id'], call['stage'], time.time(),
                                  f'Chapter {ch["number"]} recovered from saved response; call={call["id"]}'))
            logging.info('%s chapter %s %s recovered from saved response: call=%s; no new inference',
                         book['id'], ch['number'], call['stage'], call['id'])
            continue
        if call['status'] == 'reserved':
            retry_at = call['created'] + cfg['agent']['timeout_seconds'] + cfg['agent'].get('retry_cooldown_seconds', 60)
        elif call['failure_kind'] in ('quota', 'transient'):
            retry_at = call['retry_at']
        else:
            continue
        if retry_at > now:
            continue
        with store.transaction():
            store.db.execute("UPDATE calls SET status='acknowledged' WHERE id=?", (call['id'],))
            store.db.execute('UPDATE chapters SET state=?,error=NULL WHERE book_id=? AND number=?',
                             ('crawled' if field == 'translated' else 'translated', call['book_id'], call['chapter']))


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
