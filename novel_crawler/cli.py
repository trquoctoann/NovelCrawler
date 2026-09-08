import argparse
import json
import logging
from pathlib import Path
import time

from .agents import CliAgent, executable
from .config import load
from .crawler import Crawler
from .multisource import MultiSourceCrawler
from .delivery import send_book
from .epub import export_book
from .store import Store, pipeline_lock
from .translation import Translator, retry
from .runtime import ensure_config, configure_logs, report, backup_daily


def run_job(store, cfg, book, job):
    if job == 'crawl':
        crawler = MultiSourceCrawler if book.get('sources') else Crawler
        return crawler(store, cfg['crawler']).run(book, cfg['jobs']['crawl_batch'])
    if job in ('translate', 'edit'):
        return Translator(store, cfg, CliAgent(cfg['agent'])).run(book, job, cfg['jobs'][job + '_batch'])
    if job == 'export':
        return str(export_book(store, book, cfg['output_dir']))
    if job == 'send':
        return send_book(store, book, cfg)
    raise ValueError('Unknown job')


def tick(store, cfg, now=None):
    now = time.time() if now is None else now
    failures = []
    for book in cfg['books']:
        if not book['enabled']:
            continue
        for job in ('crawl', 'translate', 'edit', 'export', 'send'):
            if job in ('translate', 'edit') and not cfg['agent']['enabled']:
                continue
            if job == 'send' and not cfg['kindle']['enabled']:
                continue
            if job in ('export', 'send'):
                count = store.db.execute("SELECT COUNT(*) FROM chapters WHERE book_id=? AND state='edited'",
                                         (book['id'],)).fetchone()[0]
                if not book['completed'] or count != book['expected_chapters']:
                    continue
            due = store.db.execute('SELECT next_due FROM schedule WHERE book_id=? AND job=?', (book['id'], job)).fetchone()
            if due and due[0] > now:
                continue
            # Persist next due BEFORE dispatch: no catch-up storm after downtime/crash.
            store.db.execute('INSERT INTO schedule VALUES(?,?,?) ON CONFLICT(book_id,job) DO UPDATE SET next_due=excluded.next_due',
                             (book['id'], job, now + cfg['jobs'][job + '_interval_seconds']))
            try:
                result = run_job(store, cfg, book, job)
                logging.info('%s %s: %s', book['id'], job, result)
            except Exception as exc:
                logging.error('%s %s: %s', book['id'], job, exc)
                failures.append((book['id'], job, str(exc)))
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description='Incremental novel translation pipeline')
    parser.add_argument('--config', default='config.toml')
    commands = parser.add_subparsers(dest='command')
    start = commands.add_parser('start')
    start.add_argument('--once', action='store_true', help='Run one scheduled cycle, then exit')
    for name in ('init', 'status', 'tick', 'schedule', 'doctor', 'sources'):
        commands.add_parser(name)
    run = commands.add_parser('run')
    run.add_argument('job', choices=['crawl', 'translate', 'edit', 'export', 'send'])
    run.add_argument('--book', required=True)
    preview = commands.add_parser('preview')
    preview.add_argument('--book', required=True)
    again = commands.add_parser('retry')
    again.add_argument('--book', required=True)
    again.add_argument('--chapter', type=int, required=True)
    args = parser.parse_args(argv)
    args.command = args.command or 'start'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    ensure_config(args.config)
    cfg = load(args.config)
    configure_logs(cfg['database'])
    logging.getLogger('httpx').setLevel(logging.WARNING)
    if args.command in ('start', 'schedule'):
        logging.info('Runner started. Progress: %s; stop with Ctrl+C.', cfg['database'].parent / 'status.txt')
    if args.command == 'doctor':
        for name in ('codex', 'gemini'):
            try:
                print(name, executable(name))
            except RuntimeError as exc:
                print(exc)
        print('No inference made. Login, model access and Kindle delivery still require live verification.')
        return 0
    while True:
        # Reload on each cycle: editing config changes batch/quota without restarting.
        cfg = load(args.config)
        if args.command in ('start', 'schedule') and (cfg['database'].parent / 'stop.request').exists():
            logging.info('Graceful stop requested.')
            return 0
        with pipeline_lock(cfg['database']):
            store = Store(cfg['database'])
            try:
                for book in cfg['books']:
                    store.register(book)
                if args.command == 'init':
                    print(cfg['database'])
                elif args.command == 'status':
                    for book in cfg['books']:
                        rows = store.db.execute('SELECT state,COUNT(*) AS count FROM chapters WHERE book_id=? GROUP BY state', (book['id'],))
                        print(book['id'], json.dumps([dict(r) for r in rows], ensure_ascii=False))
                    print('calls', json.dumps(dict(store.db.execute('SELECT COUNT(*) AS count,COALESCE(SUM(reserved),0) AS reserved_tokens FROM calls').fetchone())))
                    for row in store.db.execute('SELECT book_id,number,state,error FROM chapters WHERE error IS NOT NULL'):
                        print(dict(row))
                    for row in store.db.execute('SELECT book_id,state,error FROM delivery'):
                        print(dict(row))
                    print(report(store, cfg))
                elif args.command == 'sources':
                    for book in cfg['books']:
                        if book.get('sources') and book['enabled']:
                            MultiSourceCrawler(store, cfg['crawler']).discover(book)
                    print(report(store, cfg))
                elif args.command in ('tick', 'schedule', 'start'):
                    backup_daily(store)
                    failures = tick(store, cfg)
                    report(store, cfg)
                    if failures and (args.command == 'tick' or getattr(args, 'once', False)):
                        return 1
                else:
                    book = next((b for b in cfg['books'] if b['id'] == args.book), None)
                    if not book:
                        raise ValueError('Unknown book ID')
                    if args.command == 'retry':
                        retry(store, book['id'], args.chapter)
                    elif args.command == 'preview':
                        print(export_book(store, book, cfg['output_dir'], preview=True))
                    else:
                        if not book['enabled']:
                            raise ValueError('Book disabled: qualify source and enable in config')
                        print(run_job(store, cfg, book, args.job))
            finally:
                store.close()
        if args.command not in ('schedule', 'start') or getattr(args, 'once', False):
            return 0
        time.sleep(5)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
