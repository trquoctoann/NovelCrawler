import json

import pytest

from test_pipeline import setup, seed, FakeAgent
from novel_crawler.translation import Translator, validate_result, GlossaryReviewError, recover, reserve
from novel_crawler.glossary_review import approved_readings, GlossaryDecisionRequired


GLOSSARY = {'马前': 'Mã Tiền', '王渊': 'Vương Uyên'}
SOURCE = ['主薄马前坐在堂上。', '他来到王渊马前跪下。']
TEXT = ['Chủ bạ Mã Tiền ngồi trong công đường.', 'Hắn đến trước ngựa của Vương Uyên rồi quỳ xuống.']


class MainAgent(FakeAgent):
    def generate(self, prompt):
        self.prompts.append(prompt)
        data = json.loads(prompt.split('\nDATA:\n')[1])
        return json.dumps({'title': 'Gặp nhau', 'continuity': 'Họ gặp nhau.', 'issues': [],
            'paragraphs': [{'id': p['id'], 'text': TEXT[p['id'] - 1]} for p in data['source']]}), 100


def prepare(setup, monkeypatch, verdict='ordinary', bad=None):
    s, cfg, book = setup; seed(s, book)
    s.db.execute('UPDATE chapters SET source=? WHERE number=1', (json.dumps(SOURCE, ensure_ascii=False),))
    book['glossary'].write_text(json.dumps(GLOSSARY), encoding='utf-8')
    cfg['agent']['contextual_glossary_review'] = True
    seen = []
    class Reviewer(FakeAgent):
        def __init__(self, config): super().__init__()
        def generate(self, prompt):
            seen.append(prompt)
            data = json.loads(prompt.split('\nDATA:\n')[1])
            assert len(data['source']) == 1 # No full-chapter retranslation.
            assert data['source'][0]['paragraph_id'] == 2
            row = {'id': 1, 'decision': verdict, 'source_quote': data['source'][0]['source_quote'],
                   'translation_quote': 'trước ngựa',
                   'reason': '马 là ngựa, 前 chỉ phía trước; cả cụm là vị trí trước ngựa của Vương Uyên.'}
            if bad: row[bad] = 'invented evidence'
            return json.dumps({'decisions': [row]}), 75
    monkeypatch.setattr('novel_crawler.glossary_review.CliAgent', Reviewer)
    main = MainAgent()
    return s, cfg, book, Translator(s, cfg, main), main, seen


def test_context_review_accepts_homonym_without_changing_prose_or_global_glossary(setup, monkeypatch):
    s, cfg, book, t, main, seen = prepare(setup, monkeypatch)
    assert t.run(book, 'translate', 1) == 1
    assert t.run(book, 'edit', 1) == 1
    assert len(main.prompts) == 2 and len(seen) == 1
    edited = s.db.execute('SELECT edited FROM chapters WHERE number=1').fetchone()[0]
    assert [p['text'] for p in json.loads(edited)['paragraphs']] == TEXT
    assert json.loads(book['glossary'].read_text()) == GLOSSARY
    raw = s.db.execute("SELECT response FROM calls WHERE stage='edit'").fetchone()[0]
    assert 'glossary_decisions' not in json.loads(raw)
    assert json.loads(raw)['paragraphs'] == json.loads(edited)['paragraphs']
    assert s.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 3
    proofs = approved_readings(s, book['id'], 1, SOURCE)
    assert len(proofs) == 1 and proofs[0]['paragraph_id'] == 2
    assert validate_result(edited, SOURCE, GLOSSARY, reviewed_readings=proofs)['issues'] == []
    # The same name in paragraph 1 cannot borrow the horse-context waiver.
    changed = json.loads(edited); changed['paragraphs'][0]['text'] = 'Chủ bạ ngồi trong công đường.'
    with pytest.raises(GlossaryReviewError):
        validate_result(json.dumps(changed), SOURCE, GLOSSARY, reviewed_readings=proofs)
    changed = json.loads(edited); changed['paragraphs'][1]['text'] = 'Hắn đến trước mặt Vương Uyên rồi quỳ xuống.'
    with pytest.raises(GlossaryReviewError):
        validate_result(json.dumps(changed), SOURCE, GLOSSARY, reviewed_readings=proofs)


@pytest.mark.parametrize('bad', ['source_quote', 'translation_quote', 'decision'])
def test_bad_review_evidence_never_waives_names_and_is_not_called_repeatedly(setup, monkeypatch, bad):
    s, cfg, book, t, main, seen = prepare(setup, monkeypatch, bad=bad)
    t.run(book, 'translate', 1)
    with pytest.raises(GlossaryDecisionRequired): t.run(book, 'edit', 1)
    recover(s, cfg)
    with pytest.raises(GlossaryDecisionRequired): t.run(book, 'edit', 1)
    assert len(seen) == 1 and len(main.prompts) == 2
    assert s.db.execute('SELECT COUNT(*) FROM glossary_decisions').fetchone()[0] == 0
    assert s.db.execute('SELECT edited FROM chapters WHERE number=1').fetchone()[0] is None
    assert t.run(book, 'translate', 1) == 1 # Review failure does not stop ready translations.


def test_uncertain_review_does_not_invent_a_name_or_repeat_full_chapter(setup, monkeypatch):
    s, cfg, book, t, main, seen = prepare(setup, monkeypatch, verdict='uncertain')
    t.run(book, 'translate', 1)
    with pytest.raises(GlossaryDecisionRequired, match='uncertain'): t.run(book, 'edit', 1)
    with pytest.raises(GlossaryDecisionRequired, match='uncertain'): t.run(book, 'edit', 1)
    assert len(main.prompts) == 2 and len(seen) == 1
    assert s.db.execute('SELECT COUNT(*) FROM glossary_decisions').fetchone()[0] == 0


def test_review_saying_real_name_keeps_failure_and_existing_edit_retry_bound(setup, monkeypatch):
    s, cfg, book, t, main, seen = prepare(setup, monkeypatch, verdict='name')
    t.run(book, 'translate', 1)
    with pytest.raises(GlossaryReviewError): t.run(book, 'edit', 1)
    assert len(main.prompts) == 3 # Translate + bounded original edit and repair.
    assert len(seen) == 2
    assert s.db.execute('SELECT COUNT(*) FROM glossary_decisions').fetchone()[0] == 0


def test_saved_parent_and_review_response_recover_without_another_model_call(setup, monkeypatch):
    s, cfg, book, t, main, seen = prepare(setup, monkeypatch)
    cfg['agent']['contextual_glossary_review'] = False
    cfg['limits']['max_attempts_per_chapter_stage'] = 1
    t.run(book, 'translate', 1)
    with pytest.raises(GlossaryReviewError): t.run(book, 'edit', 1)
    parent = s.db.execute("SELECT * FROM calls WHERE stage='edit'").fetchone()
    call_id = reserve(s, cfg, book['id'], 1, 'glossary_review', FakeAgent(), 'saved review',
                      now=parent['created'] + 100, parent_call_id=parent['id'])
    raw = json.dumps({'decisions': [{'id': 1, 'decision': 'ordinary', 'source_quote': SOURCE[1],
        'translation_quote': 'trước ngựa', 'reason': 'Đây là cụm vị trí trước ngựa của Vương Uyên, không phải tên người.'}]})
    s.db.execute('UPDATE calls SET response=?,usage=75 WHERE id=?', (raw, call_id))
    cfg['agent']['contextual_glossary_review'] = True
    assert t.run(book, 'edit', 1) == 1
    assert seen == [] and len(main.prompts) == 2
    assert s.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 3
    assert approved_readings(s, book['id'], 1, SOURCE)
    assert approved_readings(s, book['id'], 1, ['Different source']) == []


def test_prompt_discovers_registered_name_split_by_website_line_break(setup):
    from novel_crawler.translation import relevant_glossary
    s, cfg, book = setup
    s.db.execute('INSERT INTO book_terms(book_id,source,translation,chapter,kind) VALUES(?,?,?,?,?)',
                 (book['id'], '武穆', 'Võ Mục', 1, 'entity'))
    source = ['武', '穆气急语噎！']
    glossary = relevant_glossary(s, book['id'], {}, '\n'.join(source), 2)
    assert glossary == {'武穆': 'Võ Mục'}
    raw = {'title': 'Tức giận', 'continuity': 'Hắn tức giận.', 'issues': [], 'paragraphs': [
        {'id': 1, 'text': 'Võ Lăng', 'join_previous': False},
        {'id': 2, 'text': 'tức giận đến nghẹn lời!', 'join_previous': True}]}
    with pytest.raises(GlossaryReviewError): validate_result(json.dumps(raw), source, glossary)
    raw['paragraphs'][0]['text'] = 'Võ Mục'
    assert validate_result(json.dumps(raw), source, glossary)['issues'] == []
