import json
import threading
import time
from pathlib import Path

import pytest

from test_pipeline import setup, seed, FakeAgent
from novel_crawler.agents import MODELS, CliAgent
from novel_crawler.agent_errors import ContentPolicyError
from novel_crawler.policy_fallback import LanguageTranslator
from novel_crawler.manual import ManualInputRequired, import_submission, import_ready
from novel_crawler.translation import Translator, reserve, recover
from test_antigravity import stream


def worker(setup, fail):
    store, cfg, book = setup
    cfg['agent'].update(provider='agy', policy_fallback_provider='codex_cli',
                        workspace_dir=cfg['database'].parent / 'agy')
    seen = []
    def factory(agent_cfg):
        class Agent(FakeAgent):
            provider = agent_cfg['provider']
            model = MODELS[provider][0]
            def generate(self, prompt):
                data = json.loads(prompt.split('\nDATA:\n')[1])
                key = (self.provider, data['chapter'], 'edit' if data['draft'] else 'translate')
                seen.append(key)
                if key in fail:
                    raise fail[key]
                return super().generate(prompt)
        return Agent()
    return LanguageTranslator(store, cfg, factory, threading.Event()), seen, factory


def refusal():
    return ContentPolicyError('Blocked by content safety filters', 103526)


def test_policy_routes_only_one_chapter_and_both_stages(setup):
    store, cfg, book = setup; seed(store, book)
    t, seen, factory = worker(setup, {('agy', 1, 'translate'): refusal()})
    assert t.run(book, 'translate', 1) == 1
    # Reconstruct worker to prove chapter routing survives a manual restart.
    recover(store, cfg)
    t = LanguageTranslator(store, cfg, factory, threading.Event())
    assert t.run(book, 'edit', 1) == 1
    assert t.run(book, 'translate', 1) == 1
    assert seen == [('agy', 1, 'translate'), ('codex_cli', 1, 'translate'),
                    ('codex_cli', 1, 'edit'), ('agy', 2, 'translate')]
    call = store.db.execute('SELECT * FROM calls ORDER BY id LIMIT 1').fetchone()
    assert call['usage'] == 103526 and call['reserved'] >= 103526
    assert call['status'] == 'acknowledged' and call['failure_kind'] == 'content_blocked'
    assert not store.db.execute('SELECT * FROM provider_pauses').fetchall()
    assert cfg['agent']['provider'] == 'agy'


def test_edit_policy_falls_back_then_returns_to_gemini(setup):
    store, cfg, book = setup; seed(store, book)
    t, seen, _ = worker(setup, {('agy', 1, 'edit'): refusal()})
    assert t.run(book, 'translate', 1) == 1
    assert t.run(book, 'edit', 1) == 1
    assert t.run(book, 'translate', 1) == 1
    assert seen[-3:] == [('agy', 1, 'edit'), ('codex_cli', 1, 'edit'), ('agy', 2, 'translate')]


def test_both_refusals_create_manual_request_without_stopping_ready_edits(setup):
    store, cfg, book = setup; seed(store, book)
    t, seen, factory = worker(setup, {('agy', 2, 'translate'): refusal(), ('codex_cli', 2, 'translate'): refusal()})
    assert t.run(book, 'translate', 1) == 1
    with pytest.raises(ManualInputRequired): t.run(book, 'translate', 1)
    assert t.run(book, 'edit', 1) == 1
    request = store.db.execute('SELECT * FROM manual_requests').fetchone()
    path = Path(request['path']); original = path.read_bytes()
    assert path.exists() and path.with_name('source.json').exists()
    assert not json.loads(original)['ready']
    recover(store, cfg)
    before = len(seen)
    t = LanguageTranslator(store, cfg, factory, threading.Event())
    with pytest.raises(ManualInputRequired): t.run(book, 'translate', 1)
    assert len(seen) == before and path.read_bytes() == original
    assert not store.db.execute('SELECT * FROM final_artifacts').fetchall()


@pytest.mark.parametrize('stage', ['translate', 'edit'])
def test_manual_import_is_final_validated_and_idempotent(setup, stage):
    store, cfg, book = setup; seed(store, book)
    failures = {(p, 1, stage): refusal() for p in ('agy', 'codex_cli')}
    t, seen, _ = worker(setup, failures)
    if stage == 'edit': assert t.run(book, 'translate', 1) == 1
    with pytest.raises(ManualInputRequired): t.run(book, stage, 1)
    path = Path(store.db.execute('SELECT path FROM manual_requests').fetchone()[0])
    data = json.loads(path.read_text(encoding='utf-8'))
    # An explicit reviewed submission is a final human-edited chapter, not a
    # draft that gets sent through the refusing models again.
    data.update(ready=True, reviewed=True)
    data['result'] = {'title': 'Về nhà', 'continuity': 'Hắn nhìn sân.', 'issues': [],
                      'paragraphs': [{'id': 1, 'text': 'Hắn lặng lẽ nhìn ra ngoài sân.'}]}
    path.write_text(json.dumps(data), encoding='utf-8')
    before = store.db.execute('SELECT COUNT(*),SUM(reserved) FROM calls').fetchone()
    assert import_ready(store, cfg) == 1
    assert import_submission(store, cfg, path) == 0
    ch = store.db.execute('SELECT * FROM chapters WHERE number=1').fetchone()
    assert ch['state'] == 'edited' and ch['translated'] and ch['edited']
    assert tuple(before) == tuple(store.db.execute('SELECT COUNT(*),SUM(reserved) FROM calls').fetchone())
    assert t.run(book, 'translate', 1) == 1
    assert seen[-1] == ('agy', 2, 'translate')


@pytest.mark.parametrize('bad', ['source_hash', 'ready', 'reviewed', 'coverage', 'glossary'])
def test_manual_invalid_input_cannot_publish_or_clear_block(setup, bad):
    store, cfg, book = setup; seed(store, book, 1)
    book['glossary'].write_text(json.dumps({'院子': 'sân'}), encoding='utf-8')
    t, _, _ = worker(setup, {(p, 1, 'translate'): refusal() for p in ('agy', 'codex_cli')})
    with pytest.raises(ManualInputRequired): t.run(book, 'translate', 1)
    path = Path(store.db.execute('SELECT path FROM manual_requests').fetchone()[0])
    data = json.loads(path.read_text(encoding='utf-8')); data.update(ready=True, reviewed=True)
    data['result'].update(title='Về nhà', continuity='Hắn nhìn sân.')
    data['result']['paragraphs'][0]['text'] = 'Hắn nhìn sân.'
    if bad in ('source_hash', 'ready', 'reviewed'): data[bad] = False
    if bad == 'coverage': data['result']['paragraphs'] = []
    if bad == 'glossary': data['result']['paragraphs'][0]['text'] = 'Hắn nhìn trời.'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError): import_submission(store, cfg, path)
    assert store.db.execute('SELECT edited FROM chapters').fetchone()[0] is None
    assert store.db.execute('SELECT state FROM manual_requests').fetchone()[0] == 'pending'


@pytest.mark.parametrize('provider,error', [
    ('agy', TimeoutError('connection timed out')),
    ('agy', RuntimeError('authentication required')),
    ('agy', RuntimeError('usage limit reached')),
    ('codex_cli', RuntimeError('usage limit reached')),
])
def test_non_policy_failure_does_not_create_manual_request(setup, provider, error):
    store, cfg, book = setup; seed(store, book, 1)
    failures = {(provider, 1, 'translate'): error}
    if provider == 'codex_cli': failures[('agy', 1, 'translate')] = refusal()
    t, seen, _ = worker(setup, failures)
    with pytest.raises(type(error)): t.run(book, 'translate', 1)
    assert len(seen) == (2 if provider == 'codex_cli' else 1)
    assert not store.db.execute('SELECT * FROM manual_requests').fetchall()


def test_recover_terminal_policy_error_keeps_usage_and_routes_without_gemini_retry(setup):
    store, cfg, book = setup; seed(store, book, 1)
    t, seen, _ = worker(setup, {})
    call_id = reserve(store, cfg, book['id'], 1, 'translate', CliAgent(cfg['agent']), 'prompt', now=100000)
    call = store.db.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
    root = cfg['agent']['workspace_dir'] / 'saved'; root.mkdir(parents=True)
    (root / 'request.json').write_text(json.dumps({'call_id': call_id, 'prompt_hash': call['prompt_hash'],
        'model': call['model'], 'effort': 'high'}))
    rows = stream(); rows[1]['result'].update(status='ERROR', error='Blocked by content safety filters')
    (root / 'events.jsonl').write_text('\n'.join(map(json.dumps, rows)))
    recover(store, cfg)
    call = store.db.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
    assert call['failure_kind'] == 'content_blocked' and call['usage'] == 123
    assert t.run(book, 'translate', 1) == 1
    assert seen == [('codex_cli', 1, 'translate')]


def test_source_change_does_not_inherit_old_refusals_or_manual_request(setup):
    store, cfg, book = setup; seed(store, book, 1)
    t, _, _ = worker(setup, {(p, 1, 'translate'): refusal() for p in ('agy', 'codex_cli')})
    with pytest.raises(ManualInputRequired): t.run(book, 'translate', 1)
    store.db.execute("UPDATE chapters SET source=?,state='crawled'", (json.dumps(['他望着院子。']),))
    t, seen, _ = worker(setup, {})
    assert t.run(book, 'translate', 1) == 1
    assert seen == [('agy', 1, 'translate')]


def test_policy_reported_in_structured_result_routes_and_retains_raw(setup):
    store, cfg, book = setup; seed(store, book, 1)
    t, seen, _ = worker(setup, {})
    agent = t.primary.translator.agent
    original = agent.generate
    def blocked(prompt):
        raw, usage = original(prompt)
        data = json.loads(raw); data['issues'] = ['POLICY: I cannot translate this content.']
        return json.dumps(data), usage
    agent.generate = blocked
    assert t.run(book, 'translate', 1) == 1
    first = store.db.execute('SELECT * FROM calls ORDER BY id LIMIT 1').fetchone()
    assert first['failure_kind'] == 'content_blocked' and 'POLICY:' in first['response']
    assert seen == [('agy', 1, 'translate'), ('codex_cli', 1, 'translate')]


def test_drain_between_refusal_and_fallback_keeps_routing_for_restart(setup):
    store, cfg, book = setup; seed(store, book, 1)
    t, seen, factory = worker(setup, {})
    def blocked(prompt):
        (cfg['database'].parent / 'drain.request').write_text('stop after this response')
        raise refusal()
    t.primary.translator.agent.generate = blocked
    assert t.run(book, 'translate', 1) == 0
    assert seen == []
    (cfg['database'].parent / 'drain.request').unlink()
    t = LanguageTranslator(store, cfg, factory, threading.Event())
    assert t.run(book, 'translate', 1) == 1
    assert seen == [('codex_cli', 1, 'translate')]
