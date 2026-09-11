"""Local status reports and setup for the single-command runner."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone


_REPORT_LOCK_WARNINGS = set()


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
               'provider': cfg.get('_active_agent_provider', cfg['agent']['provider']),
               'primary_provider': cfg['agent']['provider'],
               'fallback_provider': cfg['agent'].get('fallback_provider'), 'agent_enabled': cfg['agent']['enabled'],
               'policy_fallback_provider': cfg['agent'].get('policy_fallback_provider'),
               'kindle_enabled': cfg['kindle']['enabled']}
    lines = ['NOVEL CRAWLER — Tien do', f"Agent: {summary['provider']}; primary: {summary['primary_provider']}; cap nhat UTC: {now.isoformat()}", '']
    for book in cfg['books']:
        counts = {r['state']: r['count'] for r in store.db.execute(
            'SELECT state,COUNT(*) AS count FROM chapters WHERE book_id=? GROUP BY state', (book['id'],))}
        sources = [dict(r) for r in store.db.execute(
            'SELECT source_id,state,mapped,error FROM source_scans WHERE book_id=?', (book['id'],))]
        reviews = [dict(r) for r in store.db.execute(
            'SELECT number,reason FROM source_reviews WHERE book_id=? ORDER BY number', (book['id'],))]
        summary['books'].append({'id': book['id'], 'expected': book['expected_chapters'],
                                 'states': counts, 'sources': sources, 'source_reviews': reviews})
        summary['books'][-1]['progress'] = [dict(r) for r in store.db.execute(
            'SELECT job,updated,detail FROM worker_progress WHERE book_id=?', (book['id'],))]
        lines += [f"{book['title']} ({book['expected_chapters']} chuong): {counts}",
                  f"  Nguon can kiem tra: {len(reviews)}"]
        lines += [f"  Chuong {r['number']}: {r['reason'][:160]}" for r in reviews[:10]]
        lines += [f"  {p['job']}: {p['detail']}" for p in summary['books'][-1]['progress']]
    summary['calls'] = dict(store.db.execute('SELECT COUNT(*) AS count,COALESCE(SUM(reserved),0) AS reserved_tokens FROM calls').fetchone())
    summary['today'] = dict(store.db.execute(
        'SELECT COUNT(*) AS count,COALESCE(SUM(reserved),0) AS reserved_tokens FROM calls WHERE day=?',
        (now.date().isoformat(),)).fetchone())
    summary['agent_issues'] = [dict(r) for r in store.db.execute(
        "SELECT id,book_id,chapter,stage,provider,status FROM calls WHERE status IN ('reserved','failed')")]
    summary['delivery'] = [dict(r) for r in store.db.execute('SELECT book_id,state,error FROM delivery')]
    ceilings = {k: (v or 'khong gioi han') for k, v in cfg['limits'].items()}
    lines += ['', f"Luot agent da ghi: {summary['calls']['count']} / {ceilings['calls_total']}",
              f"Hom nay UTC: {summary['today']['count']} / {ceilings['calls_per_day']} luot; "
              f"{summary['today']['reserved_tokens']} / {ceilings['reserved_tokens_per_day']} token du phong",
              f"Luot loi/chua ro: {len(summary['agent_issues'])}",
              'Kindle: ' + ('bat' if cfg['kindle']['enabled'] else 'cho cau hinh email/SMTP'),
              'Chi gui EPUB cuoi khi moi chuong da bien tap hoac duoc ghi chu thieu.']
    summary['provider_pauses'] = [dict(r) for r in store.db.execute('SELECT * FROM provider_pauses')]
    summary['manual_requests'] = [dict(r) for r in store.db.execute(
        "SELECT book_id,chapter,stage,state,path,error FROM manual_requests WHERE state='pending' ORDER BY book_id,chapter")]
    lines += [f"  Can nhap thu cong: chuong {r['chapter']} ({r['stage']}): {r['path']}"
              + (f"; loi nhap: {r['error']}" if r['error'] else '') for r in summary['manual_requests']]
    lines += [f"  {p['provider']} retry sau UTC: {datetime.fromtimestamp(p['retry_at'], timezone.utc).isoformat()}"
              for p in summary['provider_pauses'] if p['retry_at'] > now.timestamp()]
    missing = [dict(r) for r in store.db.execute(
        "SELECT book_id,number,title,summary,error FROM chapters WHERE state='missing' ORDER BY book_id,number")]
    if any(c['provider'] == cfg['agent']['provider'] and c['status'] == 'failed' for c in summary['agent_issues']):
        lines += ['Co buoc dich/bien tap dang bi chan; cac buoc du dieu kien van co the tiep tuc. Xem pipeline.log.']
    elif summary['agent_issues']:
        lines += ['Co luot agent dang xu ly hoac cho phuc hoi; reservation van duoc giu.']
    folder = Path(cfg['database']).parent
    for name, content in [('status.json', json.dumps(summary, ensure_ascii=False, indent=2)),
                          ('status.txt', '\n'.join(lines)),
                          ('missing-chapters.json', json.dumps(missing, ensure_ascii=False, indent=2))]:
        temp = folder / (name + '.tmp')
        temp.write_text(content, encoding='utf-8')
        try:
            temp.replace(folder / name)
        except PermissionError:
            # Windows viewers can open status files without delete sharing.
            # A temporarily locked progress snapshot must never cancel inference.
            if name not in _REPORT_LOCK_WARNINGS:
                logging.warning('Progress snapshot %s is locked by a reader; will retry on the next update.', name)
                _REPORT_LOCK_WARNINGS.add(name)
        else:
            _REPORT_LOCK_WARNINGS.discard(name)
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
