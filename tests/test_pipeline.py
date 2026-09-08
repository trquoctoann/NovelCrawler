import copy
import json
import sqlite3
from pathlib import Path
from zipfile import ZipFile

import pytest

from novel_crawler.config import load
from novel_crawler.store import Store, pipeline_lock
from novel_crawler.crawler import Crawler, chapter_number, parse_toc, parse_content
from novel_crawler.translation import BudgetStop, Translator, reserve, retry, validate_result
from novel_crawler.epub import export_book, verify_epub
from novel_crawler.delivery import send_book
from novel_crawler.cli import tick


@pytest.fixture
def setup(tmp_path):
    cfg = load(str(Path(__file__).parents[1] / 'config.example.toml'))
    cfg['database'] = tmp_path / 'library.db'
    cfg['output_dir'] = tmp_path / 'epub'
    cfg['agent']['enabled'] = True
    cfg['limits']['min_call_interval_seconds'] = .001
    cfg['crawler']['min_chapter_characters'] = 5
    book = cfg['books'][0]
    book.pop('sources', None)
    book.update(id='test', expected_chapters=2, enabled=True, source_url='https://example.com/book',
                toc_selector='a', content_selector='#content')
    book['glossary'] = tmp_path / 'glossary.json'
    book['glossary'].write_text('{}', encoding='utf-8')
    store = Store(cfg['database'])
    store.register(book)
    yield store, cfg, book
    store.close()


class FakeAgent:
    provider, model = 'codex_cli', 'gpt-5.6-terra'
    def __init__(self):
        self.prompts = []
    def generate(self, prompt):
        self.prompts.append(prompt)
        data = json.loads(prompt.split('\nDATA:\n')[1])
        return json.dumps({'title': 'Đêm ở làng', 'paragraphs': [
            {'id': p['id'], 'text': 'Hắn lặng lẽ nhìn ra ngoài sân.'} for p in data['source']],
            'continuity': 'Nhân vật đang ở trong nhà.', 'issues': []}, ensure_ascii=False), 100


def seed(store, book, count=2):
    for n in range(1, count + 1):
        store.db.execute('INSERT INTO chapters(book_id,number,url,title,source,state) VALUES(?,?,?,?,?,?)',
                         (book['id'], n, f'https://example.com/{n}', f'第{n}章 夜',
                          json.dumps(['他静静地望着院子。'], ensure_ascii=False), 'crawled'))


def test_resume_translation_edit_epub(setup):
    store, cfg, book = setup
    seed(store, book)
    agent = FakeAgent()
    translator = Translator(store, cfg, agent)
    assert translator.run(book, 'translate', 1) == 1
    # Re-open DB to simulate process restart.
    second = Store(cfg['database'])
    try:
        resumed = Translator(second, cfg, agent)
        assert resumed.run(book, 'translate', 2) == 1
        assert 'Nhân vật đang ở trong nhà.' in agent.prompts[1]
        assert resumed.run(book, 'translate', 2) == 0
        assert resumed.run(book, 'edit', 2) == 2
        path = export_book(second, book, cfg['output_dir'])
        assert verify_epub(path)
        with ZipFile(path) as z:
            assert b'ch-2' in z.read('OEBPS/nav.xhtml')
    finally:
        second.close()
    assert len(agent.prompts) == 4


def test_failed_call_circuit_survives_restart(setup):
    store, cfg, book = setup
    seed(store, book)
    class Failed(FakeAgent):
        def generate(self, prompt):
            raise TimeoutError('network uncertain')
    with pytest.raises(TimeoutError):
        Translator(store, cfg, Failed()).run(book, 'translate', 2)
    second = Store(cfg['database'])
    try:
        with pytest.raises(BudgetStop, match='Unresolved'):
            reserve(second, cfg, 'another-book', 1, 'translate', FakeAgent(), 'x')
        assert second.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 1
        retry(second, book['id'], 1)
        assert Translator(second, cfg, FakeAgent()).run(book, 'translate', 1) == 1
        assert second.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 2
    finally:
        second.close()


def test_lifetime_and_daily_budget_do_not_refund(setup):
    store, cfg, book = setup
    cfg['limits']['calls_total'] = 1
    reserve(store, cfg, book['id'], 1, 'translate', FakeAgent(), 'x', now=100000)
    store.db.execute("UPDATE calls SET status='acknowledged'")
    with pytest.raises(BudgetStop, match='Budget ceiling'):
        reserve(store, cfg, book['id'], 2, 'edit', FakeAgent(), 'x', now=200000)


def test_pending_reservation_stops_after_crash(setup):
    store, cfg, book = setup
    seed(store, book)
    reserve(store, cfg, book['id'], 1, 'translate', FakeAgent(), 'x', now=100000)
    with pytest.raises(BudgetStop, match='Unresolved'):
        reserve(store, cfg, book['id'], 2, 'translate', FakeAgent(), 'x', now=200000)
    retry(store, book['id'], 1)
    assert store.db.execute('SELECT status FROM calls').fetchone()[0] == 'acknowledged'


def test_max_attempts_and_interval(setup):
    store, cfg, book = setup
    cfg['limits']['min_call_interval_seconds'] = 30
    for t in (100000, 100040):
        reserve(store, cfg, book['id'], 1, 'translate', FakeAgent(), 'x', now=t)
        store.db.execute("UPDATE calls SET status='acknowledged'")
    with pytest.raises(BudgetStop, match='attempts'):
        reserve(store, cfg, book['id'], 1, 'translate', FakeAgent(), 'x', now=200000)
    with pytest.raises(BudgetStop, match='interval'):
        reserve(store, cfg, book['id'], 2, 'translate', FakeAgent(), 'x', now=100041)


def test_toc_missing_duplicate_and_order(setup):
    _, _, book = setup
    html = '<a href="/2">第二章 二</a><a href="/1">第一章 一</a><a href="/2">第二章 二</a>'
    assert [x['number'] for x in parse_toc(html, book)] == [1, 2]
    with pytest.raises(ValueError, match='incomplete'):
        parse_toc('<a href="/2">第二章 二</a>', book)
    with pytest.raises(ValueError, match='Conflicting'):
        parse_toc(html + '<a href="/other">第二章 二</a>', book)
    assert chapter_number('第一千二百六十四章') == 1264
    assert chapter_number('第一十章') == 10


def test_crawler_resumes_no_duplicate_fetch(setup, monkeypatch):
    store, cfg, book = setup
    crawler = Crawler(store, cfg['crawler'])
    fetched = []
    def fetch(url, encoding):
        fetched.append(url)
        if url.endswith('book'):
            return '<a href="/1">第一章</a><a href="/2">第二章</a>'
        return '<div id="content">他静静地望着院子。' + ('天空下起了雨。' if url.endswith('2') else '') + '</div>'
    monkeypatch.setattr(crawler, 'fetch', fetch)
    assert crawler.run(book, 1) == 1
    assert crawler.run(book, 1) == 1
    assert crawler.run(book, 2) == 0
    assert len(fetched) == 3


def test_reject_obfuscated_and_misaligned(setup):
    _, cfg, _ = setup
    with pytest.raises(ValueError, match='Obfuscated'):
        parse_content('<p>他望着天空\ue123院子。</p>', 'p', cfg['crawler'])
    raw = json.dumps({'title': 'Đêm', 'paragraphs': [], 'continuity': 'Nhà', 'issues': []})
    with pytest.raises(ValueError, match='coverage'):
        validate_result(raw, ['一段'], {})


def test_incomplete_export_and_send_blocked(setup):
    store, cfg, book = setup
    seed(store, book, 1)
    with pytest.raises(ValueError, match='every chapter'):
        export_book(store, book, cfg['output_dir'])
    cfg['kindle']['enabled'] = True
    with pytest.raises(ValueError, match='every chapter'):
        send_book(store, book, cfg)
    assert store.db.execute('SELECT COUNT(*) FROM delivery').fetchone()[0] == 0


def test_send_idempotent_and_uncertainty(setup, monkeypatch):
    store, cfg, book = setup
    seed(store, book)
    trans = Translator(store, cfg, FakeAgent())
    trans.run(book, 'translate', 2)
    trans.run(book, 'edit', 2)
    cfg['kindle'].update(enabled=True, smtp_host='smtp.example.com', sender='me@example.com', recipient='me@kindle.com')
    monkeypatch.setenv('KINDLE_SMTP_USERNAME', 'user')
    monkeypatch.setenv('KINDLE_SMTP_PASSWORD', 'secret')
    class SMTP:
        calls = 0
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, **kw): pass
        def login(self, *a): pass
        def sendmail(self, *a):
            SMTP.calls += 1
            raise TimeoutError('uncertain after transmission')
    monkeypatch.setattr('novel_crawler.delivery.smtplib.SMTP', SMTP)
    with pytest.raises(TimeoutError):
        send_book(store, book, cfg)
    assert send_book(store, book, cfg) == 'unknown'
    assert SMTP.calls == 1


def test_scheduler_no_catchup_and_multiple_books(setup, monkeypatch):
    store, cfg, book = setup
    other = copy.deepcopy(book)
    other['id'] = 'second'
    cfg['books'].append(other)
    store.register(other)
    called = []
    monkeypatch.setattr('novel_crawler.cli.run_job', lambda s, c, b, j: called.append((b['id'], j)))
    tick(store, cfg, now=100000)
    assert len(called) == 6
    tick(store, cfg, now=100001)
    assert len(called) == 6
    tick(store, cfg, now=10000000)
    assert len(called) == 12 # One batch each, not thousands of missed intervals


def test_os_lock_prevents_overlapping_workers(setup):
    _, cfg, _ = setup
    with pipeline_lock(cfg['database']):
        with pytest.raises(OSError):
            with pipeline_lock(cfg['database']):
                pytest.fail('Lock must not be reentrant')


def test_full_length_book_groups_xhtml(setup):
    store, cfg, book = setup
    # 1597 chapter anchors must remain navigable without 1597 HTML files.
    book['expected_chapters'] = 1597
    data = json.dumps({'title': 'Đêm', 'paragraphs': [{'id': 1, 'text': 'Gió thổi qua làng.'}],
                       'continuity': 'Làng', 'issues': []}, ensure_ascii=False)
    with store.transaction():
        for n in range(1, 1598):
            store.db.execute('INSERT INTO chapters(book_id,number,url,title,edited,state) VALUES(?,?,?,?,?,?)',
                             (book['id'], n, f'urn:test:{n}', '夜', data, 'edited'))
    path = export_book(store, book, cfg['output_dir'])
    with ZipFile(path) as z:
        assert len([name for name in z.namelist() if name.endswith('.xhtml')]) == 81
        assert b'#ch-1597' in z.read('OEBPS/nav.xhtml')


def test_daily_token_reservation_and_prompt_ceiling(setup):
    store, cfg, book = setup
    cfg['limits']['reserved_tokens_per_day'] = 12001
    reserve(store, cfg, book['id'], 1, 'translate', FakeAgent(), 'x', now=100000)
    store.db.execute("UPDATE calls SET status='succeeded'")
    with pytest.raises(BudgetStop, match='Budget ceiling'):
        reserve(store, cfg, book['id'], 2, 'edit', FakeAgent(), 'x', now=100040)
    cfg['agent']['max_prompt_bytes'] = 2
    with pytest.raises(BudgetStop, match='Prompt too large'):
        reserve(store, cfg, book['id'], 2, 'translate', FakeAgent(), '汉', now=200000)
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 1


def test_failed_alternate_provider_does_not_block_active_but_counts_budget(setup):
    store, cfg, book = setup
    alternate = FakeAgent()
    alternate.provider = 'gemini_cli'
    alternate.model = 'gemini-3.8-flash'
    reserve(store, cfg, book['id'], 1, 'smoke', alternate, 'x', now=100000)
    store.db.execute("UPDATE calls SET status='failed'")
    reserve(store, cfg, book['id'], 1, 'translate', FakeAgent(), 'x', now=200000)
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 2
    with pytest.raises(BudgetStop, match='Unresolved'):
        reserve(store, cfg, book['id'], 2, 'translate', alternate, 'x', now=300000)


def test_draft_warnings_reach_editor_but_final_warnings_block():
    raw = json.dumps({'title': 'Đêm', 'paragraphs': [{'id': 1, 'text': 'Hắn về nhà.'}],
                      'continuity': 'Nhà', 'issues': ['Cần kiểm tra tên.']})
    assert validate_result(raw, ['他回家。'], {}, allow_issues=True)['issues']
    with pytest.raises(ValueError, match='issues'):
        validate_result(raw, ['他回家。'], {})


def test_output_schema_enforces_full_chapter_past_100_paragraphs():
    from novel_crawler.agents import output_schema, SCHEMA
    schema = output_schema(109)['properties']['paragraphs']
    assert schema['minItems'] == schema['maxItems'] == 109
    assert schema['items']['properties']['id']['maximum'] == 109
    assert 'maxItems' not in SCHEMA['properties']['paragraphs']


def test_gmail_outbox_requires_complete_book_and_claims_once(setup, monkeypatch, capsys):
    from scripts.gmail_outbox import main as bridge
    store, cfg, book = setup
    cfg['kindle'].update(enabled=True, transport='gmail_connector', sender='sender@gmail.com', recipient='reader@kindle.com')
    seed(store, book)
    with pytest.raises(ValueError, match='every chapter'):
        send_book(store, book, cfg)
    agent = FakeAgent()
    Translator(store, cfg, agent).run(book, 'translate', 2)
    Translator(store, cfg, agent).run(book, 'edit', 2)
    # Exercise a real attachment spanning several connector chunks.
    import random
    import string
    rng = random.Random(0)
    edited = json.loads(store.db.execute('SELECT edited FROM chapters WHERE number=1').fetchone()[0])
    edited['paragraphs'][0]['text'] = ''.join(rng.choices(string.ascii_letters + ' ', k=24000))
    store.db.execute('UPDATE chapters SET edited=? WHERE number=1', (json.dumps(edited),))
    assert send_book(store, book, cfg) == 'ready'
    snapshot = cfg['database'].parent / 'outbox' / 'test.epub'
    original = snapshot.read_bytes()
    assert send_book(store, book, cfg) == 'ready'
    assert original == snapshot.read_bytes()
    monkeypatch.setattr('scripts.gmail_outbox.load', lambda *a: cfg)
    bridge(['claim', '--book', 'test'])
    capsys.readouterr()
    with pytest.raises(ValueError, match='Already claimed'):
        bridge(['claim', '--book', 'test'])
    import base64
    assert len(original) > 12288
    encoded = ''
    for offset in range(0, len(original), 6144):
        bridge(['chunk', '--book', 'test', '--offset', str(offset)])
        encoded += capsys.readouterr().out.strip()
    assert base64.urlsafe_b64decode(encoded) == original
    bridge(['ack', '--book', 'test', '--message-id', 'gmail-message-id'])
    assert send_book(store, book, cfg) == 'submitted'
    assert store.db.execute('SELECT message_id FROM delivery').fetchone()[0] == 'gmail-message-id'


def test_gmail_outbox_detects_tampered_snapshot(setup):
    from scripts.gmail_outbox import entry
    store, cfg, book = setup
    cfg['kindle'].update(enabled=True, transport='gmail_connector', sender='sender@gmail.com', recipient='reader@kindle.com')
    seed(store, book)
    Translator(store, cfg, FakeAgent()).run(book, 'translate', 2)
    Translator(store, cfg, FakeAgent()).run(book, 'edit', 2)
    send_book(store, book, cfg)
    path = cfg['database'].parent / 'outbox' / 'test.epub'
    path.write_bytes(path.read_bytes() + b'tampered')
    with pytest.raises(ValueError, match='changed'):
        entry(store, cfg, 'test')


@pytest.mark.parametrize('text,original,glossary', [
    ('Vương Nguyên về nhà.', '王渊回家。', {'王渊': 'Vương Uyên'}),
    ('Có 20 người.', '有2人。', {}),
    ('他 về nhà.', '他回家。', {}),
    ('<p>Hắn về nhà.</p>', '他回家。', {}),
])
def test_translation_quality_gates(text, original, glossary):
    raw = json.dumps({'title': 'Đêm', 'paragraphs': [{'id': 1, 'text': text}],
                      'continuity': 'Nhà', 'issues': []})
    with pytest.raises(ValueError):
        validate_result(raw, [original], glossary)
