"""Replay glossary checks locally without inference or changes to the library."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from .config import load
from .glossary import lexical_spans, mention_pattern, source_mentions
from .translation import load_rendering_aliases, validate_result
from .glossary_review import approved_readings


class ReadOnlyStore:
    def __init__(self, database):
        self.db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row


def audit(config):
    store = ReadOnlyStore(config['database'])
    result = {'chapters_checked': 0, 'edited_checked': 0, 'edited_validation_errors': [],
              'excluded_mentions': [], 'pending_responses': []}
    try:
        for book in config['books']:
            if not book['enabled']:
                continue
            base = json.loads(book['glossary'].read_text(encoding='utf-8'))
            entities = list(store.db.execute("SELECT * FROM book_terms WHERE book_id=? AND kind='entity'", (book['id'],)))
            aliases = load_rendering_aliases(book)
            for chapter in store.db.execute('SELECT * FROM chapters WHERE book_id=? AND source IS NOT NULL ORDER BY number', (book['id'],)):
                result['chapters_checked'] += 1
                source = json.loads(chapter['source'])
                glossary = {**base, **{r['source']: r['translation'] for r in entities if r['chapter'] <= chapter['number']}}
                for pid, paragraph in enumerate(source, 1):
                    accepted = {(m.start(), m.end()) for m in source_mentions(paragraph, glossary)}
                    for mention in mention_pattern(tuple(sorted(glossary))).finditer(paragraph) if glossary else ():
                        if (mention.start(), mention.end()) not in accepted:
                            result['excluded_mentions'].append({'book': book['id'], 'chapter': chapter['number'],
                                'paragraph': pid, 'source': paragraph, 'term': mention.group(),
                                'start': mention.start(), 'tokens': lexical_spans(paragraph)})
                if chapter['edited']:
                    result['edited_checked'] += 1
                    try:
                        validate_result(chapter['edited'], source, glossary, rendering_aliases=aliases,
                                        reviewed_readings=approved_readings(store, book['id'], chapter['number'], source))
                    except ValueError as exc:
                        result['edited_validation_errors'].append({'book': book['id'], 'chapter': chapter['number'], 'error': str(exc)})
            for call in store.db.execute("SELECT c.*,ch.source FROM calls c JOIN chapters ch ON ch.book_id=c.book_id AND ch.number=c.chapter WHERE c.book_id=? AND c.stage='edit' AND c.status='failed' AND c.response IS NOT NULL", (book['id'],)):
                entry = {'call': call['id'], 'chapter': call['chapter']}
                if (call['source_input_hash'] and call['source_input_hash'] != hashlib.sha256(call['source'].encode()).hexdigest()
                        or call['style_revision'] != config['agent'].get('style_revision', 'legacy')):
                    result['pending_responses'].append(dict(entry, result='superseded_input'))
                    continue
                try:
                    glossary = json.loads(call['glossary_context']) if call['glossary_context'] else base
                    validate_result(call['response'], json.loads(call['source']), glossary, rendering_aliases=aliases,
                                    reviewed_readings=approved_readings(store, book['id'], call['chapter'], json.loads(call['source'])))
                    entry['result'] = 'recoverable_without_inference'
                except ValueError as exc:
                    entry.update(result='review_required', error=str(exc))
                result['pending_responses'].append(entry)
    finally:
        store.db.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.toml')
    parser.add_argument('--output', default='data/audit/glossary-audit.json')
    args = parser.parse_args()
    result = audit(load(args.config))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({**{k: v for k, v in result.items() if k not in ('excluded_mentions', 'pending_responses')},
                      'excluded_mentions': len(result['excluded_mentions']), 'pending_responses': result['pending_responses']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
