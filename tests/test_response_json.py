import json

import pytest

from test_pipeline import setup, seed, FakeAgent
from test_antigravity import stream
from novel_crawler.antigravity import parse_output
from novel_crawler.response_json import loads
from novel_crawler.translation import Translator, recover


def test_repairs_object_separators_without_touching_prose():
    raw = r'{"text":"Giữ ,, và ,} cùng lời nói \"hắn\".",, "nested":{"id":70,},}'
    assert loads(raw) == {'text': 'Giữ ,, và ,} cùng lời nói "hắn".', 'nested': {'id': 70}}


@pytest.mark.parametrize('raw', [
    '{"text":"unfinished', '{"text":}', '{"text":"a" "id":1}',
    '{unquoted:1}', '{"items":[1,,2]}', '{,"text":"a"}',
    'A response: {"text":"a"}', '{"text":"a"} extra',
])
def test_does_not_guess_missing_content(raw):
    with pytest.raises(ValueError):
        loads(raw)


@pytest.mark.parametrize('stage', ['translate', 'edit'])
@pytest.mark.parametrize('bad_output', ['', '{"paragraphs": [unfinished'])
def test_bad_complete_output_keeps_usage_and_other_stage_running(setup, stage, bad_output):
    store, cfg, book = setup
    seed(store, book, 2)
    Translator(store, cfg, FakeAgent()).run(book, 'translate', 1)

    class Malformed(FakeAgent):
        def generate(self, prompt):
            rows = stream()
            result = rows[1]['result']
            result.pop('structured_output')
            result['response'] = bad_output
            return parse_output('\n'.join(map(json.dumps, rows)), 'gemini-3.8-flash', 'high')

    with pytest.raises(ValueError):
        Translator(store, cfg, Malformed()).run(book, stage, 1)
    failed = store.db.execute("SELECT * FROM calls WHERE status='failed'").fetchone()
    assert failed['response'] == bad_output
    assert failed['usage'] == 123 and failed['failure_kind'] == 'review'
    # Restart must retain the invalid draft without pausing the other stage.
    recover(store, cfg)
    other = 'translate' if stage == 'edit' else 'edit'
    assert Translator(store, cfg, FakeAgent()).run(book, other, 1) == 1
    assert store.db.execute('SELECT COUNT(*) FROM provider_pauses').fetchone()[0] == 0


@pytest.mark.parametrize('stage', ['translate', 'edit'])
def test_duplicate_comma_completes_without_another_agent_call(setup, stage):
    store, cfg, book = setup
    seed(store, book, 1)
    if stage == 'edit':
        Translator(store, cfg, FakeAgent()).run(book, 'translate', 1)

    class DuplicateComma(FakeAgent):
        def generate(self, prompt):
            raw, usage = super().generate(prompt)
            return raw.replace(', "paragraphs"', ',, "paragraphs"'), usage

    assert Translator(store, cfg, DuplicateComma()).run(book, stage, 1) == 1
    call = store.db.execute('SELECT * FROM calls ORDER BY id DESC LIMIT 1').fetchone()
    assert ',,' in call['response'] and call['status'] == 'succeeded'
    field = 'translated' if stage == 'translate' else 'edited'
    assert json.loads(store.db.execute(f'SELECT {field} FROM chapters').fetchone()[0])['paragraphs']
