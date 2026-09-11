import hashlib
import json
import time
from zipfile import ZipFile

import pytest

from test_pipeline import setup, seed, FakeAgent
from test_multisource import fixture
from test_manual_pipeline import prepare
from novel_crawler.translation import Translator, validate_result, recover
from novel_crawler.epub import export_book
from novel_crawler.runner import run_pipeline


def test_source_note_is_hidden_in_epub_and_cannot_hide_wrong_numbers(setup):
    s, cfg, book = setup
    seed(s, book)
    t = Translator(s, cfg, FakeAgent()); t.run(book, 'translate', 2); t.run(book, 'edit', 2)
    row = s.db.execute('SELECT edited FROM chapters WHERE number=1').fetchone()
    result = json.loads(row[0])
    result['source_notes'] = [{'paragraph_ids': [1], 'note': 'Nguyên tác nêu hai mức giá khác nhau; giữ nguyên cả hai.'}]
    validate_result(json.dumps(result), ['他静静地望着院子。'], {})
    s.db.execute('UPDATE chapters SET edited=? WHERE number=1', (json.dumps(result),))
    with ZipFile(export_book(s, book, cfg['output_dir'])) as z:
        text = z.read('OEBPS/part-0001.xhtml').decode()
        assert 'Ghi chú nguyên tác:' not in text and result['source_notes'][0]['note'] not in text
    result['paragraphs'][0]['text'] = 'Giá 20 đồng.'
    with pytest.raises(ValueError, match='numeric literals'):
        validate_result(json.dumps(result), ['价格2元。'], {})
    result['source_notes'][0]['paragraph_ids'] = [999]
    with pytest.raises(ValueError, match='numeric literals'):
        validate_result(json.dumps(result), ['价格2元。'], {})


def test_reviewed_response_recovery_keeps_raw_and_checks_hashes(setup):
    s, cfg, book = setup
    seed(s, book)
    t = Translator(s, cfg, FakeAgent()); t.run(book, 'translate', 1)
    class Editor(FakeAgent):
        def generate(self, prompt):
            raw, usage = super().generate(prompt)
            result = json.loads(raw); result['issues'] = ['Source inconsistency']
            return json.dumps(result), usage
    with pytest.raises(ValueError, match='Agent reported issues'):
        Translator(s, cfg, Editor()).run(book, 'edit', 1)
    call = s.db.execute("SELECT * FROM calls WHERE stage='edit'").fetchone()
    source = s.db.execute('SELECT source FROM chapters WHERE number=1').fetchone()[0]
    reviewed = json.loads(call['response']); reviewed['issues'] = []
    reviewed['source_notes'] = [{'paragraph_ids': [1], 'note': 'Ghi chú đã đối chiếu với nguyên tác.'}]
    s.db.execute('INSERT INTO reviewed_responses VALUES(?,?,?,?,?,?)', (
        call['id'], json.dumps(reviewed), hashlib.sha256(call['response'].encode()).hexdigest(),
        'wrong-hash', 'Reviewed source', time.time()))
    recover(s, cfg)
    assert s.db.execute('SELECT edited FROM chapters WHERE number=1').fetchone()[0] is None
    s.db.execute('UPDATE reviewed_responses SET source_hash=?', (hashlib.sha256(source.encode()).hexdigest(),))
    recover(s, cfg)
    assert json.loads(s.db.execute('SELECT edited FROM chapters WHERE number=1').fetchone()[0])['source_notes']
    assert s.db.execute('SELECT response FROM calls WHERE id=?', (call['id'],)).fetchone()[0] == call['response']
    assert s.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 3
    assert not s.db.execute("SELECT 1 FROM calls WHERE status IN ('reserved','failed')").fetchone()


def test_editor_failure_allows_translation_beyond_priority_threshold(fixture, monkeypatch):
    class Agent(FakeAgent):
        def generate(self, prompt):
            raw, usage = super().generate(prompt)
            payload = json.loads(prompt.split('\nDATA:\n')[1])
            if payload['draft']:
                result = json.loads(raw); result['issues'] = ['Unresolved translation problem']
                raw = json.dumps(result)
            return raw, usage
    store, cfg, book, pages, fetched = prepare(fixture, monkeypatch, Agent)
    cfg['jobs'].update(edit_priority_backlog=1, translate_batch=1)
    for key in ('calls_per_day', 'calls_total', 'reserved_tokens_per_day', 'reserved_tokens_total'):
        cfg['limits'][key] = 0
    assert run_pipeline(store, cfg)
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 5
    assert store.db.execute('SELECT COUNT(*) FROM chapters WHERE translated IS NOT NULL').fetchone()[0] == 3
    assert store.db.execute('SELECT COUNT(*) FROM chapters WHERE source IS NOT NULL').fetchone()[0] == 3
    assert store.db.execute("SELECT state FROM chapters WHERE number=1").fetchone()[0] == 'edit_failed'
    assert not list(cfg['output_dir'].glob('*.epub'))


def test_reported_concern_gets_source_review_instead_of_immediate_stop(setup):
    s, cfg, book = setup
    seed(s, book, 1)
    s.db.execute('UPDATE chapters SET source=?', (json.dumps(['他一脸神王。']),))
    class Agent(FakeAgent):
        def generate(self, prompt):
            payload = json.loads(prompt.split('\nDATA:\n')[1])
            result = {'title': 'Mong mỏi', 'paragraphs': [{'id': 1, 'text': 'Vẻ mặt hắn đầy mong mỏi.'}],
                      'issues': [], 'continuity': 'Hắn mong mỏi.', 'source_notes': []}
            if payload.get('repair_findings'):
                assert 'Agent reported issues' in payload['repair_findings']
                assert payload['draft']['issues']
                result['source_notes'] = [{'paragraph_ids': [1], 'note': 'Nguyên tác có dấu hiệu gõ nhầm; diễn đạt theo ngữ cảnh.'}]
            elif payload['draft']:
                result['issues'] = ['Nguồn có thể gõ nhầm, đã đọc theo ngữ cảnh.']
            return json.dumps(result), 100
    t = Translator(s, cfg, Agent()); t.run(book, 'translate', 1)
    assert t.run(book, 'edit', 1) == 1
    result = json.loads(s.db.execute('SELECT edited FROM chapters').fetchone()[0])
    assert not result['issues'] and result['source_notes']
    rows = s.db.execute("SELECT status,response FROM calls WHERE stage='edit' ORDER BY id").fetchall()
    assert [r['status'] for r in rows] == ['acknowledged', 'succeeded']
    assert json.loads(rows[0]['response'])['issues'] # Original concern is never silently erased.


def test_reported_concerns_do_not_skip_other_validation():
    from novel_crawler.translation import EditingReviewError
    raw = {'title': 'Giá', 'paragraphs': [{'id': 1, 'text': 'Giá 20 đồng.'}],
           'continuity': 'Giá tiền.', 'issues': ['Uncertain source expression.']}
    with pytest.raises(EditingReviewError) as caught:
        validate_result(json.dumps(raw), ['价格2元。'], {})
    assert any('Agent reported issues' in f for f in caught.value.findings)
    assert any('numeric literals changed' in f for f in caught.value.findings)
    raw['paragraphs'] = []
    with pytest.raises(ValueError, match='coverage'):
        validate_result(json.dumps(raw), ['价格2元。'], {})


def test_optional_notes_do_not_waste_another_editor_call(setup):
    s,cfg,book=setup;seed(s,book,1)
    class Agent(FakeAgent):
        def generate(self,prompt):
            raw,usage=super().generate(prompt);d=json.loads(raw)
            d['source_notes']=[{'paragraph_ids':[999],'note':'Tác giả dùng hai mức giá khác nhau: 原文.'}]
            return json.dumps(d),usage
    t=Translator(s,cfg,Agent())
    assert t.run(book,'translate',1)==1 and t.run(book,'edit',1)==1
    assert s.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==2
    assert not s.db.execute("SELECT 1 FROM calls WHERE status='failed'").fetchone()


def test_source_concerns_are_advisory_but_real_translation_errors_still_block():
    raw={'title':'Giá cá','paragraphs':[{'id':1,'text':'Giá 2 đồng.'}],
         'continuity':'Bán cá.','issues':['SOURCE: Hai đoạn nguồn nêu giá khác nhau.']}
    assert validate_result(json.dumps(raw),['价格2元。'],{})['issues']==[]
    raw['paragraphs'][0]['text']='Giá 20 đồng.'
    with pytest.raises(ValueError,match='numeric literals'):
        validate_result(json.dumps(raw),['价格2元。'],{})
    raw['paragraphs'][0]['text']='Giá 2 đồng.';raw['issues']=['Missing translated sentence.']
    with pytest.raises(ValueError,match='Agent reported issues'):
        validate_result(json.dumps(raw),['价格2元。'],{})


def test_completed_preview_has_distinct_identity_and_does_not_freeze_final_delivery(setup):
    import xml.etree.ElementTree as ET
    s,cfg,book=setup;seed(s,book)
    t=Translator(s,cfg,FakeAgent());t.run(book,'translate',1);t.run(book,'edit',1)
    path=export_book(s,book,cfg['output_dir'],preview=True)
    with ZipFile(path) as z:
        opf=ET.fromstring(z.read('OEBPS/content.opf'))
        ns={'dc':'http://purl.org/dc/elements/1.1/'}
        assert opf.find('.//dc:identifier',ns).text.endswith(':chapters-1-1')
        assert opf.find('.//dc:title',ns).text.endswith('Chương 1–1')
        assert 'ch-2' not in z.read('OEBPS/nav.xhtml').decode()
    assert not s.db.execute('SELECT 1 FROM final_artifacts').fetchone()
    assert not s.db.execute('SELECT 1 FROM delivery').fetchone()
    with pytest.raises(ValueError,match='every chapter'):export_book(s,book,cfg['output_dir'])
