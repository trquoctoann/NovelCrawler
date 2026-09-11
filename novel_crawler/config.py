from pathlib import Path
import re
import json
import tomllib


def load(path: str) -> dict:
    source = Path(path).resolve()
    with source.open('rb') as f:
        cfg = tomllib.load(f)
    cfg['_root'] = source.parent
    for key in ('database', 'output_dir'):
        cfg[key] = source.parent / cfg[key]
    ids = set()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', cfg['agent'].get('style_revision', 'legacy')):
        raise ValueError('style_revision must be a safe, nonempty revision name')
    for book in cfg['books']:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', book['id']) or book['id'] in ids:
            raise ValueError('Book IDs must be unique safe slugs')
        ids.add(book['id'])
        if not isinstance(book.get('contextual_terms', []), list) or any(
                not isinstance(term, str) or not term for term in book.get('contextual_terms', [])):
            raise ValueError('contextual_terms must be a list of source terms')
        if book['expected_chapters'] < 1:
            raise ValueError('expected_chapters must be positive')
        for flag in ('literal_titles', 'allow_missing_chapters'):
            if flag in book and type(book[flag]) is not bool:
                raise ValueError(f'{flag} must be a boolean')
        if book.get('toc_mode', 'numbered') not in ('numbered', 'ordered'):
            raise ValueError('Unknown TOC mode')
        if book.get('reference_manifest'):
            manifest_path = source.parent / book['reference_manifest']
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if (manifest['original_title'] != book['original_title'] or manifest['author'] != book['author']
                    or manifest['source_url'] != book['source_url']):
                raise ValueError('Reference manifest identifies a different book or edition')
            entries = manifest['chapters']
            if ([e['number'] for e in entries] != list(range(1, book['expected_chapters'] + 1))
                    or any(type(e['word_count']) is not int or e['word_count'] < 1 for e in entries)
                    or len({e['url'] for e in entries}) != len(entries)):
                raise ValueError('Reference manifest must contain complete unique chapters and word counts')
            book['_reference_entries'] = entries
        if book.get('toc_mode') == 'ordered' and not book.get('_reference_entries'):
            raise ValueError('Ordered TOC requires reference_manifest')
        book['glossary'] = source.parent / book['glossary']
        if book.get('rendering_aliases'):
            book['rendering_aliases'] = source.parent / book['rendering_aliases']
        # The manual pipeline applies the same persisted retry/fallback rules to
        # books with one configured source as to books with several sources.
        if not book.get('sources'):
            book['sources'] = [dict(id='primary', **{k: book[k] for k in (
                'source_url', 'toc_selector', 'content_selector', 'encoding', 'toc_mode') if k in book})]
        source_ids = set()
        for entry in book.get('sources', []):
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', entry['id']) or entry['id'] in source_ids:
                raise ValueError('Source IDs must be unique safe slugs within a book')
            source_ids.add(entry['id'])
            entry.setdefault('literal_titles', book.get('literal_titles', False))
            if entry.get('reference_titles') and not book.get('_reference_entries'):
                raise ValueError('Source reference_titles requires a pinned reference manifest')
            if entry.get('toc_mode', 'numbered') not in ('numbered', 'ordered', 'jjwxc'):
                raise ValueError('Unknown source TOC mode')
            for key in ('source_url', 'toc_selector', 'encoding'):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ValueError(f'Source requires {key}')
            if entry.get('adapter', 'css') not in ('css', 'piaotia'):
                raise ValueError('Unknown content adapter')
            if entry.get('adapter', 'css') == 'css' and not entry.get('content_selector'):
                raise ValueError('CSS source requires content_selector')
    unlimited = {'calls_per_day', 'calls_total', 'reserved_tokens_per_day', 'reserved_tokens_total'}
    for section in ('limits', 'jobs'):
        if any(type(v) is not int or v < (0 if section == 'limits' and k in unlimited else 1)
               for k, v in cfg[section].items()):
            raise ValueError(f'{section}: limits must be positive integers (0 disables aggregate budgets only)')
    for section, keys in {
        'agent': ['timeout_seconds', 'max_prompt_bytes', 'max_output_tokens', 'max_output_bytes'],
        'crawler': ['request_interval_seconds', 'timeout_seconds', 'max_response_bytes',
                    'min_chapter_characters', 'max_chapter_characters'],
        'kindle': ['max_message_bytes'],
    }.items():
        if any(cfg[section][k] <= 0 for k in keys):
            raise ValueError(f'{section}: limits must be positive')
    if cfg['agent']['provider'] not in ('codex_cli', 'gemini_cli', 'agy'):
        raise ValueError('Unsupported agent provider')
    if type(cfg['agent'].get('contextual_glossary_review', False)) is not bool:
        raise ValueError('contextual_glossary_review must be a boolean')
    if cfg['agent'].get('fallback_provider') is not None and (
            cfg['agent']['provider'] != 'codex_cli' or cfg['agent']['fallback_provider'] != 'agy'):
        raise ValueError('Only codex_cli primary with agy quota fallback is supported')
    if cfg['agent'].get('policy_fallback_provider') is not None and (
            cfg['agent']['provider'] != 'agy' or cfg['agent']['policy_fallback_provider'] != 'codex_cli'):
        raise ValueError('Policy fallback requires agy primary and codex_cli fallback')
    cfg['agent']['workspace_dir'] = source.parent / 'data' / 'agent-workspaces' / 'agy'
    if cfg['kindle'].get('transport', 'smtp') not in ('smtp', 'gmail_connector'):
        raise ValueError('Unsupported delivery transport')
    for key in ('quota_cooldown_seconds', 'retry_cooldown_seconds'):
        if key in cfg['agent'] and (type(cfg['agent'][key]) is not int or cfg['agent'][key] <= 0):
            raise ValueError(f'agent: {key} must be a positive integer')
    return cfg
