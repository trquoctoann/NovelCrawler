"""One manual invocation: independent crawl and language workers, then final delivery."""
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time

from .agents import CliAgent
from .completion import finalize_missing, is_complete
from .crawler import Crawler
from .delivery import send_book
from .epub import export_book
from .multisource import MultiSourceCrawler
from .runtime import backup_daily, report
from .store import Store
from .translation import Translator, recover
from .style import apply_style_revision
from .policy_fallback import LanguageTranslator
from .manual import import_ready


def checkpoint(store, book, job, detail):
    store.db.execute('INSERT OR REPLACE INTO worker_progress VALUES(?,?,?,?)',
                     (book['id'], job, time.time(), str(detail)[:1000]))


def crawl_progress(store, book):
    return tuple(tuple(tuple(r) for r in store.db.execute(query, (book['id'],))) for query in (
        "SELECT number,CASE WHEN source IS NOT NULL THEN 'accepted' ELSE state END "
        "FROM chapters WHERE book_id=? ORDER BY number",
        'SELECT id,state,attempts FROM source_candidates WHERE book_id=? ORDER BY id',
        'SELECT source_id,state,attempts FROM source_scans WHERE book_id=? ORDER BY source_id'))


def run_pipeline(store, cfg, once=False, stop=None):
    """Caller owns pipeline_lock for the entire run; each thread owns its connection.

    No scheduler or automatic daily reset loop. Finish available work, or return
    when only cooldowns, quota or human review remain. Crawl keeps working even
    when the agent is paused. ``once`` limits each worker to one configured batch.
    """
    stop = stop or threading.Event()
    crawled = threading.Event()
    failures = []
    books = [b for b in cfg['books'] if b['enabled']]
    recover(store, cfg)
    for book in books:
        apply_style_revision(store, cfg, book)
    import_ready(store, cfg)
    backup_daily(store)

    def crawl_worker():
        local = Store(cfg['database'])
        try:
            for book in books:
                if stop.is_set():
                    break
                crawler_type = MultiSourceCrawler if book.get('sources') else Crawler
                crawler = crawler_type(local, cfg['crawler'])
                crawler.stop = stop
                while not stop.is_set():
                    try:
                        before = crawl_progress(local, book)
                        count = crawler.run(book, cfg['jobs']['crawl_batch'])
                        missing = finalize_missing(local, book)
                        changed = crawl_progress(local, book) != before
                        checkpoint(local, book, 'crawl', f'accepted={count}; missing={missing}')
                    except InterruptedError:
                        checkpoint(local, book, 'crawl', 'Stopped by request; progress saved')
                        logging.info('%s crawl stopped by request; progress saved', book['id'])
                        break
                    except Exception as exc:
                        checkpoint(local, book, 'crawl', exc)
                        failures.append((book['id'], 'crawl', str(exc)))
                        logging.exception('%s crawl stopped', book['id'])
                        break
                    if once or not changed:
                        break
        finally:
            local.close()
            crawled.set()

    def language_worker():
        local = Store(cfg['database'])
        disabled = set()
        attempts = set()
        try:
            if not cfg['agent']['enabled']:
                return
            translator = LanguageTranslator(local, cfg, CliAgent, stop)
            while not stop.is_set():
                if (cfg['database'].parent / 'drain.request').exists():
                    stop.set()
                    break
                producer_done = crawled.is_set()
                progress = 0
                for book in books:
                    backlog = local.db.execute('SELECT COUNT(*) FROM chapters WHERE book_id=? '
                        'AND translated IS NOT NULL AND edited IS NULL', (book['id'],)).fetchone()[0]
                    threshold = cfg['jobs'].get('edit_priority_backlog', cfg['jobs'].get('max_unedited_chapters', 8))
                    prefer_edit = not once and backlog >= threshold and (book['id'], 'edit') not in disabled
                    if prefer_edit:
                        checkpoint(local, book, 'translate',
                                   f'Editing prioritized; draft backlog={backlog}; translation resumes if editing is blocked')
                    stages = ('edit', 'translate') if prefer_edit else ('translate', 'edit')
                    for stage in stages:
                        key = (book['id'], stage)
                        if (cfg['database'].parent / 'drain.request').exists():
                            stop.set() # Checked between calls; retain the current paid response.
                        if stop.is_set() or key in disabled or (once and key in attempts):
                            continue
                        batch = cfg['jobs'][stage + '_batch']
                        try:
                            count = translator.run(book, stage, batch)
                            if count or producer_done:
                                attempts.add(key)
                            progress += count
                            checkpoint(local, book, stage, f'completed={count}')
                            if prefer_edit and stage == 'edit' and count:
                                break # Drain edits first; zero progress or an edit error falls through to translation.
                        except InterruptedError:
                            checkpoint(local, book, stage, 'Stopped by request; reservation retained')
                            logging.info('%s %s stopped by request; reservation retained', book['id'], stage)
                            disabled.add(key)
                        except Exception as exc:
                            disabled.add(key)
                            failures.append((book['id'], stage, str(exc)))
                            checkpoint(local, book, stage, exc)
                            logging.warning('%s %s paused: %s', book['id'], stage, exc)
                if not progress and producer_done:
                    break
                if not progress:
                    stop.wait(.2)
        finally:
            local.close()

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='novel')
    futures = [pool.submit(crawl_worker), pool.submit(language_worker)]
    try:
        while not all(f.done() for f in futures):
            if (cfg['database'].parent / 'stop.request').exists():
                stop.set()
            report(store, cfg)
            stop.wait(.5) if not stop.is_set() else time.sleep(.1)
        for future in futures:
            future.result()
    finally:
        if any(not f.done() for f in futures):
            stop.set()
        pool.shutdown(wait=True)
        report(store, cfg)
    if not stop.is_set():
        for book in books:
            if is_complete(store, book):
                try:
                    path = export_book(store, book, cfg['output_dir'])
                    checkpoint(store, book, 'export', path)
                    result = send_book(store, book, cfg)
                    checkpoint(store, book, 'send', result)
                    if result in ('ready', 'sending', 'unknown'):
                        failures.append((book['id'], 'send', f'Delivery pending: {result}'))
                except Exception as exc:
                    failures.append((book['id'], 'send', str(exc)))
                    checkpoint(store, book, 'send', exc)
                    logging.exception('%s final delivery stopped', book['id'])
    report(store, cfg)
    return failures
