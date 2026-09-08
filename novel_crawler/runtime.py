"""Local status reports and setup for the single-command runner."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone


def ensure_config(path):
    target = Path(path).resolve()
    if target.exists():
        return target
    example = Path(__file__).resolve().parents[1] / 'config.example.toml'
    if target.parent != example.parent:
        raise ValueError('First run: create config in the project directory so relative glossary paths are correct')
    # Exclusive creation prevents concurrent launchers overwriting configuration.
    try:
        with target.open('x', encoding='utf-8') as dest:
            dest.write(example.read_text(encoding='utf-8'))
    except FileExistsError:
        pass
    return target


def configure_logs(database):
    folder = Path(database).parent
    folder.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        handler = RotatingFileHandler(folder / 'pipeline.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        root.addHandler(handler)


def report(store, cfg):
    now = datetime.now(timezone.utc)
    summary = {'updated_at': now.isoformat(), 'books': [], 'limits': cfg['limits'],
               'provider': cfg['agent']['provider'], 'agent_enabled': cfg['agent']['enabled'],
               'kindle_enabled': cfg['kindle']['enabled']}
    lines = ['NOVEL CRAWLER — Tien do', f"Agent: {cfg['agent']['provider']}; cap nhat UTC: {now.isoformat()}", '']
    for book in cfg['books']:
        counts = {r['state']: r['count'] for r in store.db.execute(
            'SELECT state,COUNT(*) AS count FROM chapters WHERE book_id=? GROUP BY state', (book['id'],))}
        sources = [dict(r) for r in store.db.execute(
            'SELECT source_id,state,mapped,error FROM source_scans WHERE book_id=?', (book['id'],))]
        reviews = [dict(r) for r in store.db.execute(
            'SELECT number,reason FROM source_reviews WHERE book_id=? ORDER BY number', (book['id'],))]
        summary['books'].append({'id': book['id'], 'expected': book['expected_chapters'],
                                 'states': counts, 'sources': sources, 'source_reviews': reviews})
        lines += [f"{book['title']} ({book['expected_chapters']} chuong): {counts}",
                  f"  Nguon can kiem tra: {len(reviews)}"]
        lines += [f"  Chuong {r['number']}: {r['reason'][:160]}" for r in reviews[:10]]
    summary['calls'] = dict(store.db.execute('SELECT COUNT(*) AS count,COALESCE(SUM(reserved),0) AS reserved_tokens FROM calls').fetchone())
    summary['today'] = dict(store.db.execute(
        'SELECT COUNT(*) AS count,COALESCE(SUM(reserved),0) AS reserved_tokens FROM calls WHERE day=?',
        (now.date().isoformat(),)).fetchone())
    summary['agent_issues'] = [dict(r) for r in store.db.execute(
        "SELECT id,book_id,chapter,stage,provider,status FROM calls WHERE status IN ('reserved','failed')")]
    summary['delivery'] = [dict(r) for r in store.db.execute('SELECT book_id,state,error FROM delivery')]
    lines += ['', f"Luot agent da ghi: {summary['calls']['count']} / {cfg['limits']['calls_total']}",
              f"Hom nay UTC: {summary['today']['count']} / {cfg['limits']['calls_per_day']} luot; "
              f"{summary['today']['reserved_tokens']} / {cfg['limits']['reserved_tokens_per_day']} token du phong",
              f"Luot loi/chua ro: {len(summary['agent_issues'])}",
              'Kindle: ' + ('bat' if cfg['kindle']['enabled'] else 'cho cau hinh email/SMTP'),
              'Chi gui mot EPUB khi toan bo chuong da bien tap.']
    if any(c['provider'] == cfg['agent']['provider'] for c in summary['agent_issues']):
        lines += ['Agent tam dung de tranh lap loi va ton quota; xem pipeline.log.']
    folder = Path(cfg['database']).parent
    for name, content in [('status.json', json.dumps(summary, ensure_ascii=False, indent=2)), ('status.txt', '\n'.join(lines))]:
        temp = folder / (name + '.tmp')
        temp.write_text(content, encoding='utf-8')
        temp.replace(folder / name)
    return '\n'.join(lines)


def backup_daily(store):
    from datetime import date
    import sqlite3
    folder = store.path.parent / 'backups'
    folder.mkdir(exist_ok=True)
    target = folder / f'{store.path.stem}-{date.today().isoformat()}.db'
    if not target.exists():
        temp = target.with_suffix('.tmp')
        from contextlib import closing
        with closing(sqlite3.connect(temp)) as dest:
            store.db.backup(dest)
        temp.replace(target)
    # Retain local backups, no implicit deletions of historical user data.
