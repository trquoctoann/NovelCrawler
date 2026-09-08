from pathlib import Path
import re
import tomllib


def load(path: str) -> dict:
    source = Path(path).resolve()
    with source.open('rb') as f:
        cfg = tomllib.load(f)
    cfg['_root'] = source.parent
    for key in ('database', 'output_dir'):
        cfg[key] = source.parent / cfg[key]
    ids = set()
    for book in cfg['books']:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', book['id']) or book['id'] in ids:
            raise ValueError('Book IDs must be unique safe slugs')
        ids.add(book['id'])
        if book['expected_chapters'] < 1:
            raise ValueError('expected_chapters must be positive')
        book['glossary'] = source.parent / book['glossary']
        source_ids = set()
        for entry in book.get('sources', []):
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', entry['id']) or entry['id'] in source_ids:
                raise ValueError('Source IDs must be unique safe slugs within a book')
            source_ids.add(entry['id'])
            for key in ('source_url', 'toc_selector', 'encoding'):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ValueError(f'Source requires {key}')
            if entry.get('adapter', 'css') not in ('css', 'piaotia'):
                raise ValueError('Unknown content adapter')
            if entry.get('adapter', 'css') == 'css' and not entry.get('content_selector'):
                raise ValueError('CSS source requires content_selector')
    for section in ('limits', 'jobs'):
        if any(type(v) is not int or v <= 0 for v in cfg[section].values()):
            raise ValueError(f'{section}: limits and intervals must be positive integers')
    for section, keys in {
        'agent': ['timeout_seconds', 'max_prompt_bytes', 'max_output_tokens', 'max_output_bytes'],
        'crawler': ['request_interval_seconds', 'timeout_seconds', 'max_response_bytes',
                    'min_chapter_characters', 'max_chapter_characters'],
        'kindle': ['max_message_bytes'],
    }.items():
        if any(cfg[section][k] <= 0 for k in keys):
            raise ValueError(f'{section}: limits must be positive')
    if cfg['agent']['provider'] not in ('codex_cli', 'gemini_cli'):
        raise ValueError('Unsupported agent provider')
    if cfg['kindle'].get('transport', 'smtp') not in ('smtp', 'gmail_connector'):
        raise ValueError('Unsupported delivery transport')
    return cfg
