import json

import pytest

from test_pipeline import setup, seed, FakeAgent
from novel_crawler.style import apply_style_revision
from novel_crawler.translation import Translator, reserve, recover, BudgetStop
from novel_crawler.completion import is_complete


def test_new_style_archives_old_edits_and_reuses_them_as_drafts(setup):
    s, cfg, book = setup
    seed(s, book)
    t = Translator(s, cfg, FakeAgent())
    t.run(book, 'translate', 2); t.run(book, 'edit', 2)
    old = s.db.execute('SELECT edited FROM chapters WHERE number=1').fetchone()[0]
    assert is_complete(s, book)
    cfg['agent']['style_revision'] = 'natural-vi-v1'
    assert apply_style_revision(s, cfg, book) == 2
    assert not is_complete(s, book)
    assert s.db.execute('SELECT translated FROM chapters WHERE number=1').fetchone()[0] == old
    assert s.db.execute('SELECT edited FROM edited_history WHERE number=1').fetchone()[0] == old
    assert apply_style_revision(s, cfg, book) == 0
    agent = FakeAgent()
    assert Translator(s, cfg, agent).run(book, 'edit', 2) == 2
    assert json.loads(agent.prompts[0].split('\nDATA:\n')[1])['draft'] == json.loads(old)
    assert is_complete(s, book)
    assert s.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 6
    assert 'NATURAL VIETNAMESE' in agent.prompts[0]


def test_style_attempt_budget_is_per_revision_and_cost_is_not_refunded(setup):
    s, cfg, book = setup
    for i in range(2):
        reserve(s, cfg, book['id'], 1, 'edit', FakeAgent(), 'x', now=100000+i*100)
        s.db.execute("UPDATE calls SET status='succeeded'")
    cfg['agent']['style_revision'] = 'natural-vi-v1'
    reserve(s, cfg, book['id'], 1, 'edit', FakeAgent(), 'x', now=200000)
    assert s.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 3
    s.db.execute("UPDATE calls SET status='succeeded'")
    cfg['limits']['calls_total'] = 3
    with pytest.raises(BudgetStop, match='Budget ceiling'):
        reserve(s, cfg, book['id'], 2, 'edit', FakeAgent(), 'x', now=300000)


def test_old_edit_response_cannot_recover_as_new_style(setup):
    s, cfg, book = setup
    seed(s, book)
    t = Translator(s, cfg, FakeAgent())
    t.run(book, 'translate', 2); t.run(book, 'edit', 1)
    s.db.execute("UPDATE calls SET status='failed' WHERE stage='edit'")
    cfg['agent']['style_revision'] = 'natural-vi-v1'
    apply_style_revision(s, cfg, book)
    recover(s, cfg)
    assert s.db.execute('SELECT edited FROM chapters WHERE number=1').fetchone()[0] is None
    assert s.db.execute("SELECT status FROM calls WHERE stage='edit'").fetchone()[0] == 'acknowledged'
