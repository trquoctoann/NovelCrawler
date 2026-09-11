import json

import pytest

from test_pipeline import setup, seed, FakeAgent
from novel_crawler.translation import Translator, EditingReviewError, editing_draft, recover


def test_numeric_repair_collects_all_paragraphs_and_preserves_call_history(setup):
    s, cfg, book = setup
    seed(s, book, 1)
    s.db.execute('UPDATE chapters SET source=?', (json.dumps(['价格2元。', '有3人。']),))
    class Agent(FakeAgent):
        def generate(self, prompt):
            data = json.loads(prompt.split('\nDATA:\n')[1])
            repair = data.get('repair_findings')
            if repair:
                assert 'Paragraph 1' in repair and 'Paragraph 2' in repair
            wrong = data['draft'] is not None and not repair
            return json.dumps({'title': 'Giá cả', 'paragraphs': [
                {'id': 1, 'text': 'Giá 20 đồng.' if wrong else 'Giá 2 đồng.'},
                {'id': 2, 'text': 'Có 30 người.' if wrong else 'Có 3 người.'}],
                'issues': [], 'continuity': 'Giá và số người.'}), 100
    t = Translator(s, cfg, Agent())
    t.run(book, 'translate', 1)
    assert t.run(book, 'edit', 1) == 1
    assert [r[0] for r in s.db.execute('SELECT status FROM calls ORDER BY id')] == ['succeeded','acknowledged','succeeded']
    assert s.db.execute('SELECT failure_kind FROM calls WHERE id=2').fetchone()[0] == 'quality'
    recover(s, cfg)
    assert s.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 3


def test_numeric_repair_stops_at_attempt_cap(setup):
    s, cfg, book = setup
    seed(s, book, 1)
    s.db.execute('UPDATE chapters SET source=?', (json.dumps(['价格2元。']),))
    class Agent(FakeAgent):
        def generate(self, prompt):
            return json.dumps({'title': 'Giá', 'paragraphs': [{'id': 1, 'text': 'Giá 20 đồng.'}],
                               'issues': [], 'continuity': 'Giá tiền.'}), 100
    t = Translator(s, cfg, Agent()); t.run(book, 'translate', 1)
    with pytest.raises(EditingReviewError, match='numeric literals'):
        t.run(book, 'edit', 1)
    recover(s, cfg)
    assert t.run(book, 'edit', 1) == 0
    assert s.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 3
    assert s.db.execute('SELECT edited FROM chapters').fetchone()[0] is None


def test_obsolete_numeric_warning_is_recomputed_without_touching_prose():
    raw = {'title': 'Bạc', 'paragraphs': [{'id': 1, 'text': 'Có 50 lạng bạc.'}],
           'continuity': 'Tiền bạc.', 'issues': ['Paragraph 1: numeric literals changed; correct against the supplied source']}
    result = editing_draft(json.dumps(raw), ['有五十两银子。'], {}, {})
    assert result['issues'] == []
    assert result['paragraphs'] == raw['paragraphs']
