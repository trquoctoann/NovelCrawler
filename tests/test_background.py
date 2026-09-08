import json
import os
from pathlib import Path
import subprocess
import sys
import time


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Background lifecycle timed out')
        time.sleep(.05)


def test_background_is_single_instance_and_stops_without_external_calls(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = (root / 'config.example.toml').read_text(encoding='utf-8')
    (tmp_path / 'config.toml').write_text(config.replace('enabled = true', 'enabled = false'), encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONUTF8='1')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    def control(*args):
        return subprocess.run([sys.executable, '-m', 'scripts.background', *args],
                              cwd=tmp_path, env=env, capture_output=True, text=True,
                              timeout=10, check=True, creationflags=flags).stdout
    record = tmp_path / 'data' / 'runner.json'
    try:
        assert 'launched' in control()
        wait_for(record.exists)
        pid = json.loads(record.read_text())['pid']
        assert 'already active' in control()
        assert json.loads(record.read_text())['pid'] == pid
        wait_for((tmp_path / 'data' / 'status.json').exists)
        status = json.loads((tmp_path / 'data' / 'status.json').read_text(encoding='utf-8'))
        assert status['calls']['count'] == 0
        control('stop')
        wait_for(lambda: not record.exists())
    finally:
        if record.exists():
            control('stop')
            wait_for(lambda: not record.exists())
