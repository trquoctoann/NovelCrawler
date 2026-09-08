"""Run the existing scheduler in one detached process, with graceful stop."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from novel_crawler.config import load
from novel_crawler.store import pipeline_lock


def main():
    cfg = load('config.toml')
    folder = cfg['database'].parent
    folder.mkdir(exist_ok=True)
    guard = str(cfg['database']) + '.background'
    stop = folder / 'stop.request'
    if sys.argv[1:] == ['stop']:
        stop.write_text('Stop after current batch.', encoding='utf-8')
        print('Stop requested; runner will finish the current batch safely.')
        return
    if sys.argv[1:] == ['worker']:
        lock = pipeline_lock(guard)
        try:
            lock.__enter__()
        except OSError:
            raise SystemExit('Another background runner already owns the lock')
        try:
            stop.unlink(missing_ok=True)
            (folder / 'runner.json').write_text(json.dumps({'pid': os.getpid(), 'started': time.time()}))
            from novel_crawler.cli import main as pipeline
            pipeline(['start'])
        finally:
            (folder / 'runner.json').unlink(missing_ok=True)
            lock.__exit__(None, None, None)
        return
    try:
        with pipeline_lock(guard):
            pass
    except OSError:
        print('Background runner already active.')
        return
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == 'nt' else 0
    with (folder / 'runner-console.log').open('ab') as out:
        child = subprocess.Popen([sys.executable, '-m', 'scripts.background', 'worker'],
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env,
                                 creationflags=flags, start_new_session=os.name != 'nt', close_fds=True)
    print(f'Background runner launched: PID {child.pid}. Progress: {folder / "status.txt"}')


if __name__ == '__main__':
    main()
