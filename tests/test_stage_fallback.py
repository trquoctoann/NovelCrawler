import json

import pytest

from test_pipeline import setup, seed, FakeAgent
from test_multisource import fixture
from test_manual_pipeline import prepare
from novel_crawler.runner import run_pipeline
from novel_crawler.translation import Translator, BudgetStop, recover, reserve


@pytest.mark.parametrize('bug', [KeyError('edit field'), TypeError('edit schema'), AttributeError('edit field')])
def test_local_edit_bug_does_not_block_translation(fixture, monkeypatch, bug):
    class Agent(FakeAgent):
        def generate(self, prompt):
            data = json.loads(prompt.split('\nDATA:\n')[1])
            if data['draft']:
                raise bug
            return super().generate(prompt)
    store, cfg, book, _, _ = prepare(fixture, monkeypatch, Agent)
    cfg['jobs']['edit_priority_backlog'] = 1
    assert run_pipeline(store, cfg)
    assert store.db.execute('SELECT COUNT(*) FROM chapters WHERE translated IS NOT NULL').fetchone()[0] == 3
    assert store.db.execute("SELECT COUNT(*) FROM calls WHERE stage='edit'").fetchone()[0] == 1
    assert store.db.execute("SELECT failure_kind FROM calls WHERE stage='edit'").fetchone()[0] == 'internal'
    assert not list(cfg['output_dir'].glob('*.epub'))
    # A restart does not loop over a failed editor or retranslate committed work.
    before = store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
    run_pipeline(store, cfg)
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == before


def test_high_backlog_edits_first_and_low_backlog_returns_to_translation(fixture, monkeypatch):
    from novel_crawler.multisource import MultiSourceCrawler
    observed = []
    class Agent(FakeAgent):
        def generate(self, prompt):
            data = json.loads(prompt.split('\nDATA:\n')[1])
            observed.append(('edit' if data['draft'] else 'translate', data['chapter']))
            return super().generate(prompt)
    store, cfg, book, _, _ = prepare(fixture, monkeypatch, Agent)
    MultiSourceCrawler(store, cfg['crawler']).run(book, 3)
    Translator(store, cfg, FakeAgent()).run(book, 'translate', 2)
    cfg['jobs']['edit_priority_backlog'] = 2
    assert run_pipeline(store, cfg) == []
    assert observed == [('edit', 1), ('translate', 3), ('edit', 2), ('edit', 3)]


def test_source_correction_resets_only_input_attempts_and_ignores_old_editor(setup):
    store, cfg, book = setup
    seed(store, book, 1)
    Translator(store, cfg, FakeAgent()).run(book, 'translate', 1)
    class Bad(FakeAgent):
        def generate(self, prompt):
            raw, usage = super().generate(prompt)
            data = json.loads(raw); data['issues'] = ['Old source is repeated']
            return json.dumps(data), usage
    with pytest.raises(ValueError):
        Translator(store, cfg, Bad()).run(book, 'edit', 1)
    old = [dict(r) for r in store.db.execute("SELECT * FROM calls WHERE stage='edit'")]
    assert len(old) == 2
    with pytest.raises(BudgetStop, match='attempts'):
        reserve(store, cfg, book['id'], 1, 'edit', FakeAgent(), 'same source')
    store.db.execute("UPDATE chapters SET source=?,state='translated'", (json.dumps(['他望着窗外。']),))
    recover(store, cfg)
    assert store.db.execute('SELECT edited FROM chapters').fetchone()[0] is None
    class New(FakeAgent):
        def generate(self, prompt):
            data = json.loads(prompt.split('\nDATA:\n')[1])
            assert 'repair_findings' not in data
            assert not data['draft']['issues']
            return super().generate(prompt)
    assert Translator(store, cfg, New()).run(book, 'edit', 1) == 1
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 4
    for row in old:
        saved = store.db.execute('SELECT * FROM calls WHERE id=?', (row['id'],)).fetchone()
        assert saved['response'] == row['response']
        assert saved['reserved'] == row['reserved']
        assert saved['status'] == 'acknowledged'


def test_edit_quota_still_pauses_same_account_translation(fixture, monkeypatch):
    from novel_crawler.multisource import MultiSourceCrawler
    attempted = []
    class Agent(FakeAgent):
        def generate(self, prompt):
            data = json.loads(prompt.split('\nDATA:\n')[1])
            attempted.append('edit' if data['draft'] else 'translate')
            raise RuntimeError('429 usage limit reached')
    store, cfg, book, _, _ = prepare(fixture, monkeypatch, Agent)
    MultiSourceCrawler(store, cfg['crawler']).run(book, 3)
    Translator(store, cfg, FakeAgent()).run(book, 'translate', 1)
    cfg['jobs']['edit_priority_backlog'] = 1
    assert run_pipeline(store, cfg)
    assert attempted == ['edit']
    assert store.db.execute('SELECT COUNT(*) FROM chapters WHERE translated IS NOT NULL').fetchone()[0] == 1
    assert not list(cfg['output_dir'].glob('*.epub'))
