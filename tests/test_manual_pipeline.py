import json
import threading
import time
from zipfile import ZipFile

import httpx
import pytest

from test_pipeline import setup, seed, FakeAgent
from test_multisource import fixture
from novel_crawler.completion import finalize_missing, is_complete
from novel_crawler.epub import export_book, verify_epub
from novel_crawler.runner import run_pipeline
from novel_crawler.store import Store, pipeline_lock
from novel_crawler.translation import BudgetStop, Translator, recover, reserve


def prepare(fixture, monkeypatch, agent=FakeAgent):
    store, cfg, book, crawler, pages, fetched = fixture
    cfg['output_dir'] = cfg['database'].parent / 'epub'
    cfg['jobs'].update(crawl_batch=1, translate_batch=1, edit_batch=1)
    cfg['limits']['min_call_interval_seconds'] = .001
    book['glossary'] = cfg['database'].parent / 'glossary.json'
    book['glossary'].write_text('{}', encoding='utf-8')
    def fetch(self, url, encoding):
        fetched.append(url)
        result = pages[url]
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr('novel_crawler.multisource.MultiSourceCrawler.fetch', fetch)
    monkeypatch.setattr('novel_crawler.runner.CliAgent', lambda cfg: agent())
    return store, cfg, book, pages, fetched


def test_manual_workers_actually_overlap_and_resume_without_calls(fixture, monkeypatch):
    translating = threading.Event()
    class Agent(FakeAgent):
        def generate(self, prompt):
            translating.set()
            return super().generate(prompt)
    store, cfg, book, pages, fetched = prepare(fixture, monkeypatch, Agent)
    from novel_crawler.multisource import MultiSourceCrawler
    original = MultiSourceCrawler.fetch
    def fetch(self, url, encoding):
        if url == 'https://primary.test/2':
            assert translating.wait(5), 'Translation must start while crawling is still in progress'
        return original(self, url, encoding)
    monkeypatch.setattr(MultiSourceCrawler, 'fetch', fetch)
    with pipeline_lock(cfg['database']):
        assert run_pipeline(store, cfg) == []
        assert is_complete(store, book)
        assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 6
        path = export_book(store, book, cfg['output_dir'])
        digest = verify_epub(path)
        before = list(fetched)
        assert run_pipeline(store, cfg) == []
        assert fetched == before
        assert verify_epub(path) == digest
        assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 6
        jobs = {r[0] for r in store.db.execute('SELECT job FROM worker_progress')}
        assert jobs == {'crawl', 'translate', 'edit', 'export', 'send'}


def test_missing_is_not_marked_until_catalog_retries_exhausted(fixture):
    store, cfg, book, crawler, pages, fetched = fixture
    pages['https://primary.test/1'] = '<h1>第1章 归家</h1><article>短</article>'
    pages['https://backup.test/toc'] = httpx.ReadTimeout('temporary')
    assert crawler.run(book, 3) == 2
    assert finalize_missing(store, book) == 0
    assert store.db.execute('SELECT state FROM chapters WHERE number=1').fetchone()[0] == 'source_waiting'
    for _ in range(2):
        store.db.execute('UPDATE source_scans SET retry_at=0')
        crawler.run(book, 3)
    assert finalize_missing(store, book) == 1
    assert store.db.execute('SELECT state FROM chapters WHERE number=1').fetchone()[0] == 'missing'
    assert fetched.count('https://backup.test/toc') == 3


def test_drain_finishes_current_response_before_stopping_and_resumes(fixture, monkeypatch):
    class Agent(FakeAgent):
        calls = 0
        def generate(self, prompt):
            Agent.calls += 1
            if Agent.calls == 1:
                (fixture[1]['database'].parent / 'drain.request').write_text('Drain for maintenance')
            return super().generate(prompt)
    store,cfg,book,pages,fetched = prepare(fixture,monkeypatch,Agent)
    run_pipeline(store,cfg)
    assert Agent.calls == 1
    assert store.db.execute('SELECT COUNT(*) FROM chapters WHERE translated IS NOT NULL').fetchone()[0] == 1
    assert not store.db.execute("SELECT 1 FROM calls WHERE status IN ('failed','reserved')").fetchone()
    (cfg['database'].parent / 'drain.request').unlink()
    assert run_pipeline(store,cfg) == []
    assert is_complete(store,book) and Agent.calls == 6


def test_missing_middle_chapter_notes_context_and_final_delivery(fixture, monkeypatch):
    prompts = []
    class Agent(FakeAgent):
        def generate(self, prompt):
            prompts.append(json.loads(prompt.split('\nDATA:\n')[1]))
            return super().generate(prompt)
    store, cfg, book, pages, fetched = prepare(fixture, monkeypatch, Agent)
    for host, number in [('primary', 2), ('backup', 102)]:
        pages[f'https://{host}.test/{number}'] = f'<h1>第{number}章 夜话</h1><article>短</article>'
    cfg['kindle'].update(enabled=True, smtp_host='smtp.test', sender='sender@test.com', recipient='reader@kindle.com')
    monkeypatch.setenv('KINDLE_SMTP_USERNAME', 'test')
    monkeypatch.setenv('KINDLE_SMTP_PASSWORD', 'test')
    class SMTP:
        calls = 0
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, **kw): pass
        def login(self, *a): pass
        def sendmail(self, *a):
            assert is_complete(store, book)
            SMTP.calls += 1
            return {}
    monkeypatch.setattr('novel_crawler.delivery.smtplib.SMTP', SMTP)
    assert run_pipeline(store, cfg) == []
    assert [r[0] for r in store.db.execute('SELECT state FROM chapters ORDER BY number')] == ['edited', 'missing', 'edited']
    assert len(prompts) == 4
    last = [p for p in prompts if p['chapter'] == 3]
    assert all(p['missing_previous_chapters'] == [2] for p in last)
    assert all(p['previous_context']['continuity'] == 'Nhân vật đang ở trong nhà.' for p in last)
    with ZipFile(export_book(store, book, cfg['output_dir'])) as z:
        assert 'Chương 2: Chương bị thiếu' in z.read('OEBPS/nav.xhtml').decode()
        assert 'Không suy đoán nội dung.' in z.read('OEBPS/part-0001.xhtml').decode()
    missing = json.loads((cfg['database'].parent / 'missing-chapters.json').read_text(encoding='utf-8'))
    assert [m['number'] for m in missing] == [2]
    assert run_pipeline(store, cfg) == []
    assert SMTP.calls == 1
    assert len(list(cfg['output_dir'].glob('*.epub'))) == 1


def test_quota_does_not_stop_crawler_and_no_immediate_retry(fixture, monkeypatch):
    class Exhausted(FakeAgent):
        calls = 0
        def generate(self, prompt):
            Exhausted.calls += 1
            raise RuntimeError('429 usage limit reached')
    store, cfg, book, pages, fetched = prepare(fixture, monkeypatch, Exhausted)
    assert run_pipeline(store, cfg)
    assert store.db.execute('SELECT COUNT(*) FROM chapters WHERE source IS NOT NULL').fetchone()[0] == 3
    assert Exhausted.calls == 1
    assert run_pipeline(store, cfg) == [] # Persisted pause, no eligible work.
    assert Exhausted.calls == 1
    assert not list(cfg['output_dir'].glob('*.epub'))
    store.db.execute('UPDATE calls SET retry_at=0')
    store.db.execute('UPDATE provider_pauses SET retry_at=0')
    monkeypatch.setattr('novel_crawler.runner.CliAgent', lambda cfg: FakeAgent())
    assert run_pipeline(store, cfg) == []
    assert is_complete(store, book)
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 7


def test_cooldown_waiting_sources_return_without_busy_loop(fixture, monkeypatch):
    store, cfg, book, pages, fetched = prepare(fixture, monkeypatch)
    pages['https://primary.test/toc'] = httpx.ReadTimeout('offline')
    pages['https://backup.test/toc'] = httpx.ReadTimeout('offline')
    assert run_pipeline(store, cfg) == []
    assert len(fetched) == 3 # Canonical and one attempt per catalog.
    assert not is_complete(store, book)
    assert not store.db.execute("SELECT 1 FROM chapters WHERE state='missing'").fetchone()


def test_recover_durable_response_without_inference(setup):
    store, cfg, book = setup
    seed(store, book)
    agent = FakeAgent()
    raw, _ = agent.generate('rules\nDATA:\n' + json.dumps({'source': [{'id': 1, 'text': '他静静地望着院子。'}]}))
    call = reserve(store, cfg, book['id'], 1, 'translate', agent, 'original prompt', now=1000)
    store.db.execute('UPDATE calls SET response=? WHERE id=?', (raw, call))
    store.db.execute("UPDATE chapters SET state='translate_inflight' WHERE number=1")
    with pipeline_lock(cfg['database']):
        second = Store(cfg['database'])
        try:
            recover(second, cfg)
            assert second.db.execute('SELECT state FROM chapters WHERE number=1').fetchone()[0] == 'translated'
            assert second.db.execute('SELECT status FROM calls').fetchone()[0] == 'succeeded'
            assert Translator(second, cfg, agent).run(book, 'translate', 2) == 1
        finally:
            second.close()
    assert len(agent.prompts) == 2


def test_waiting_gap_does_not_starve_later_available_chapters(fixture, monkeypatch):
    store, cfg, book, pages, fetched = prepare(fixture, monkeypatch)
    pages['https://backup.test/toc'] = httpx.ReadTimeout('offline')
    pages['https://primary.test/1'] = httpx.ReadTimeout('temporary')
    assert run_pipeline(store, cfg) == []
    assert store.db.execute("SELECT COUNT(*) FROM chapters WHERE state='crawled'").fetchone()[0] == 2
    assert store.db.execute('SELECT state FROM chapters WHERE number=1').fetchone()[0] == 'source_waiting'
    assert fetched.count('https://primary.test/1') == 1


def test_interrupted_call_retries_next_run_with_lifetime_cap(setup):
    store, cfg, book = setup
    seed(store, book)
    reserve(store, cfg, book['id'], 1, 'translate', FakeAgent(), 'x', now=1000)
    store.db.execute("UPDATE chapters SET state='translate_inflight' WHERE number=1")
    recover(store, cfg, now=1001)
    assert store.db.execute('SELECT status FROM calls').fetchone()[0] == 'reserved'
    recover(store, cfg, now=2000)
    assert Translator(store, cfg, FakeAgent()).run(book, 'translate', 1) == 1
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 2
    store.db.execute("UPDATE chapters SET translated=NULL,state='crawled' WHERE number=1")
    with pytest.raises(BudgetStop, match='attempts'):
        Translator(store, cfg, FakeAgent()).run(book, 'translate', 1)


def test_invalid_durable_response_is_not_retried(setup):
    store, cfg, book = setup
    seed(store, book)
    call = reserve(store, cfg, book['id'], 1, 'translate', FakeAgent(), 'x', now=1000)
    store.db.execute('UPDATE calls SET response=? WHERE id=?', ('{"partial":', call))
    recover(store, cfg)
    assert store.db.execute('SELECT failure_kind FROM calls').fetchone()[0] == 'review'
    agent = FakeAgent()
    assert Translator(store, cfg, agent).run(book, 'translate', 1) == 0
    assert agent.prompts == []


def test_daily_budget_restart_keeps_reservations(setup):
    store, cfg, book = setup
    seed(store, book)
    cfg['limits']['calls_per_day'] = 1
    translator = Translator(store, cfg, FakeAgent())
    assert translator.run(book, 'translate', 1) == 1
    recover(store, cfg)
    with pytest.raises(BudgetStop, match='Budget ceiling'):
        translator.run(book, 'translate', 1)
    store.db.execute("UPDATE calls SET day='2000-01-01'")
    assert translator.run(book, 'translate', 1) == 1
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 2


def test_smtp_login_failure_is_safe_to_retry(setup, monkeypatch):
    from novel_crawler.delivery import send_book
    store, cfg, book = setup
    seed(store, book)
    translator = Translator(store, cfg, FakeAgent())
    translator.run(book, 'translate', 2)
    translator.run(book, 'edit', 2)
    cfg['kindle'].update(enabled=True, smtp_host='smtp.test', sender='s@test.com', recipient='r@kindle.com')
    monkeypatch.setenv('KINDLE_SMTP_USERNAME', 'test')
    monkeypatch.setenv('KINDLE_SMTP_PASSWORD', 'test')
    class SMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, **kw): pass
        def login(self, *a): raise ConnectionError('Login unavailable')
    monkeypatch.setattr('novel_crawler.delivery.smtplib.SMTP', SMTP)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            send_book(store, book, cfg)
    assert not store.db.execute('SELECT 1 FROM delivery').fetchone()


def test_final_artifact_immutable_and_tamper_detected(setup):
    store, cfg, book = setup
    seed(store, book)
    translator = Translator(store, cfg, FakeAgent())
    translator.run(book, 'translate', 2)
    translator.run(book, 'edit', 2)
    path = export_book(store, book, cfg['output_dir'])
    original = path.read_bytes()
    assert export_book(store, book, cfg['output_dir']).read_bytes() == original
    path.write_bytes(original + b'changed')
    with pytest.raises(ValueError, match='modified'):
        export_book(store, book, cfg['output_dir'])


def test_stop_signal_interrupts_agent_process(tmp_path):
    import os
    import sys
    from novel_crawler.agents import run_process
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        run_process([sys.executable, '-c', 'import time; time.sleep(30)'], '', tmp_path,
                    60, 1000, os.environ.copy(), stop)


def test_new_terms_survive_distant_chapters_and_conflicts_are_rejected(setup):
    from novel_crawler.translation import validate_result
    store, cfg, book = setup
    seed(store, book)
    store.db.execute('UPDATE chapters SET source=?', (json.dumps(['林风走进院子。']),))
    class Agent(FakeAgent):
        def generate(self, prompt):
            self.prompts.append(json.loads(prompt.split('\nDATA:\n')[1]))
            return json.dumps({'title': 'Gió', 'paragraphs': [{'id': 1, 'text': 'Lâm Phong bước vào sân.'}],
                               'continuity': 'Một người về nhà.', 'issues': [],
                               'terms': [{'source': '林风', 'translation': 'Lâm Phong'}]}, ensure_ascii=False), 100
    agent = Agent()
    assert Translator(store, cfg, agent).run(book, 'translate', 2) == 2
    assert agent.prompts[1]['preferred_terms']['林风'] == 'Lâm Phong'
    assert '林风' not in agent.prompts[1]['glossary']
    assert Translator(store, cfg, agent).run(book, 'edit', 2) == 2
    assert agent.prompts[-1]['glossary']['林风'] == 'Lâm Phong'
    assert store.db.execute('SELECT translation FROM book_terms').fetchone()[0] == 'Lâm Phong'
    bad = {'title': 'Gió', 'paragraphs': [{'id': 1, 'text': 'Lâm Phong bước vào sân.'}],
           'continuity': 'Một người về nhà.', 'issues': [],
           'terms': [{'source': '林风', 'translation': 'Lâm Phụng'}]}
    assert validate_result(json.dumps(bad), ['林风走进院子。'], {'林风': 'Lâm Phong'})['terms'] == []


def test_repeated_quota_errors_keep_global_cost_but_allow_later_translation(setup):
    store, cfg, book = setup
    seed(store, book)
    class Agent(FakeAgent):
        def generate(self, prompt):
            raise RuntimeError('RESOURCE_EXHAUSTED quota')
    for _ in range(3):
        with pytest.raises(RuntimeError, match='quota'):
            Translator(store, cfg, Agent()).run(book, 'translate', 1)
        store.db.execute('UPDATE calls SET retry_at=0')
        store.db.execute('UPDATE provider_pauses SET retry_at=0')
        recover(store, cfg)
    assert Translator(store, cfg, FakeAgent()).run(book, 'translate', 1) == 1
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 4


def test_optional_registry_paraphrase_does_not_discard_valid_prose():
    from novel_crawler.translation import validate_result
    raw = json.dumps({'title': 'Gặp gỡ', 'paragraphs': [{'id': 1, 'text': 'Đồng sinh là lại dịch ở phòng hộ tịch.'}],
                      'continuity': 'Hai chức danh.', 'issues': [], 'terms': [
                          {'source': '童生', 'translation': 'đồng sinh'},
                          {'source': '户房胥吏', 'translation': 'lại dịch phòng hộ tịch'}]}, ensure_ascii=False)
    result = validate_result(raw, ['童生是户房胥吏。'], {'童生': 'đồng sinh'})
    assert result['paragraphs'][0]['text'] == 'Đồng sinh là lại dịch ở phòng hộ tịch.'
    assert result['terms'] == [{'source': '童生', 'translation': 'đồng sinh'}]


def test_embedded_line_breaks_keep_paragraph_coverage_and_content_gates():
    from novel_crawler.translation import validate_result
    raw = {'title': 'Trở về', 'paragraphs': [{'id': 1, 'text': 'Nàng đứng đợi.\n\nChàng trở về.'}],
           'continuity': 'Chàng đã về.', 'issues': []}
    result = validate_result(json.dumps(raw), ['她等着。他回来了。'], {})
    assert result['paragraphs'] == [{'id': 1, 'text': 'Nàng đứng đợi.\n\nChàng trở về.'}]
    for invalid in ('Nàng đợi.\n他回来了。', 'Nàng đợi.\n<p>Chàng về.</p>', '\n\n'):
        raw['paragraphs'][0]['text'] = invalid
        with pytest.raises(ValueError):
            validate_result(json.dumps(raw), ['她等着。他回来了。'], {})


def test_bad_draft_does_not_block_editing_prior_ready_chapter(setup):
    store, cfg, book = setup
    seed(store, book)
    assert Translator(store, cfg, FakeAgent()).run(book, 'translate', 1) == 1
    class BadDraft(FakeAgent):
        def generate(self, prompt):
            return '{"unfinished":', 100
    with pytest.raises(ValueError):
        Translator(store, cfg, BadDraft()).run(book, 'translate', 1)
    assert Translator(store, cfg, FakeAgent()).run(book, 'edit', 2) == 1
    assert store.db.execute('SELECT state FROM chapters WHERE number=1').fetchone()[0] == 'edited'
    assert store.db.execute('SELECT state FROM chapters WHERE number=2').fetchone()[0] == 'translate_failed'
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 3


def test_locked_status_file_cannot_cancel_pipeline(setup, monkeypatch):
    from pathlib import Path
    from novel_crawler.runtime import report
    store, cfg, book = setup
    original = Path.replace
    def locked(path, target):
        if path.name == 'status.txt.tmp':
            raise PermissionError('Windows viewer denies delete sharing')
        return original(path, target)
    monkeypatch.setattr(Path, 'replace', locked)
    assert 'NOVEL CRAWLER' in report(store, cfg)
    assert (cfg['database'].parent / 'status.json').exists()
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 0


def test_draft_quality_findings_reach_editor_and_final_remains_strict(setup):
    from novel_crawler.translation import validate_result
    store, cfg, book = setup
    book.pop('rendering_aliases', None) # Exercise an unverified variant, not the book-specific approved alias.
    seed(store, book)
    store.db.execute('UPDATE chapters SET source=? WHERE number=1', (json.dumps(['大王庄有2人。']),))
    book['glossary'].write_text(json.dumps({'大王庄': 'Đại Vương trang'}, ensure_ascii=False), encoding='utf-8')
    bad = {'title': 'Trong làng', 'paragraphs': [{'id': 1, 'text': 'Thôn Tiểu Vương có 20 người.'}],
           'continuity': 'Dân làng.', 'issues': [], 'terms': [
               {'source': '大王庄', 'translation': 'Thôn Đại Vương'}]}
    class Draft(FakeAgent):
        def generate(self, prompt):
            return json.dumps(bad, ensure_ascii=False), 100
    assert Translator(store, cfg, Draft()).run(book, 'translate', 1) == 1
    assert not store.db.execute('SELECT 1 FROM book_terms').fetchone()
    class Editor(FakeAgent):
        def generate(self, prompt):
            data = json.loads(prompt.split('\nDATA:\n')[1])
            assert any('glossary mismatch' in issue for issue in data['draft']['issues'])
            assert any('numeric literals changed' in issue for issue in data['draft']['issues'])
            return json.dumps({'title': 'Trong làng', 'paragraphs': [{'id': 1, 'text': 'Đại Vương trang có 2 người.'}],
                               'continuity': 'Dân làng.', 'issues': []}, ensure_ascii=False), 100
    assert Translator(store, cfg, Editor()).run(book, 'edit', 1) == 1
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 2
    with pytest.raises(ValueError):
        validate_result(json.dumps(bad), ['大王庄有2人。'], {'大王庄': 'Đại Vương trang'})


def test_chinese_remainder_is_editable_but_not_publishable():
    from novel_crawler.translation import validate_result
    raw = json.dumps({'title': 'Trở về', 'paragraphs': [{'id': 1, 'text': 'Hắn 回家.'}],
                      'continuity': 'Về nhà.', 'issues': []})
    assert validate_result(raw, ['他回家。'], {}, allow_issues=True)['issues']
    with pytest.raises(ValueError):
        validate_result(raw, ['他回家。'], {})


def test_verified_rendering_alias_is_scoped_to_exact_source_term():
    from novel_crawler.translation import validate_result
    aliases = {'大王庄': ['thôn Đại Vương']}
    glossary = {'大王庄': 'Đại Vương trang'}
    raw = json.dumps({'title': 'Dân làng', 'paragraphs': [{'id': 1, 'text': 'Dân thôn Đại Vương về nhà.'}],
                      'continuity': 'Dân làng.', 'issues': [], 'terms': [
                          {'source': '大王庄', 'translation': 'thôn Đại Vương'}]}, ensure_ascii=False)
    result = validate_result(raw, ['大王庄村民回家。'], glossary, rendering_aliases=aliases)
    assert result['paragraphs'][0]['text'] == 'Dân Đại Vương trang về nhà.'
    assert result['terms'][0]['translation'] == 'Đại Vương trang'
    different = json.loads(raw)
    different['terms'] = []
    result = validate_result(json.dumps(different), ['大王村村民回家。'], glossary, rendering_aliases=aliases)
    assert result['paragraphs'][0]['text'] == 'Dân thôn Đại Vương về nhà.'
    different['paragraphs'][0]['text'] = 'Dân thôn Tiểu Vương về nhà.'
    with pytest.raises(ValueError, match='glossary mismatch'):
        validate_result(json.dumps(different), ['大王庄村民回家。'], glossary, rendering_aliases=aliases)


def test_contextual_vocabulary_is_guidance_but_names_remain_locked(setup):
    from novel_crawler.translation import relevant_glossary, save_terms, validate_result
    store, cfg, book = setup
    save_terms(store, book['id'], 1, {'terms': [
        {'source': '市坊', 'translation': 'thị phường', 'kind': 'preferred'},
        {'source': '王渊', 'translation': 'Vương Uyên', 'kind': 'entity'}]})
    glossary = relevant_glossary(store, book['id'], {}, '王渊说市坊之间。')
    assert glossary == {'王渊': 'Vương Uyên'}
    raw = {'title': 'Trong thành', 'paragraphs': [{'id': 1, 'text': 'Vương Uyên nói: giữa thị và phường.'}],
           'continuity': 'Trong thành.', 'issues': []}
    assert validate_result(json.dumps(raw), ['王渊说市坊之间。'], glossary)
    raw['paragraphs'][0]['text'] = 'Vương Nguyên nói: giữa thị và phường.'
    with pytest.raises(ValueError, match='glossary mismatch'):
        validate_result(json.dumps(raw), ['王渊说市坊之间。'], glossary)


def test_cancellation_kills_descendant_processes(tmp_path):
    import os
    import sys
    from novel_crawler.agents import run_process
    stop = threading.Event()
    started = tmp_path / 'child-started'
    escaped = tmp_path / 'escaped'
    child = ('from pathlib import Path; import time; '
             f'Path({str(started)!r}).touch(); time.sleep(1.5); Path({str(escaped)!r}).touch()')
    parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)'
    def cancel():
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        stop.set()
    timer = threading.Thread(target=cancel)
    timer.start()
    try:
        with pytest.raises(InterruptedError):
            run_process([sys.executable, '-c', parent], '', tmp_path, 10, 1000, os.environ.copy(), stop)
    finally:
        timer.join()
    assert started.exists()
    time.sleep(1.7)
    assert not escaped.exists(), 'A CLI descendant survived cancellation'


def test_default_cli_exits_without_scheduler_and_status_works_under_lock(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    config = (root / 'config.example.toml').read_text(encoding='utf-8')
    (tmp_path / 'config.toml').write_text(config.replace('enabled = true', 'enabled = false'), encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONUTF8='1')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    command = [sys.executable, '-m', 'novel_crawler']
    subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, timeout=10,
                   check=True, creationflags=flags)
    with pipeline_lock(tmp_path / 'data/library.db'):
        result = subprocess.run(command + ['status'], cwd=tmp_path, env=env, capture_output=True,
                                timeout=10, check=True, creationflags=flags)
        assert b'NOVEL CRAWLER' in result.stdout
    local = Store(tmp_path / 'data/library.db')
    try:
        assert local.db.execute('SELECT COUNT(*) FROM schedule').fetchone()[0] == 0
        assert local.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 0
    finally:
        local.close()
