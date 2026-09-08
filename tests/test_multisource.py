import copy
import json
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from novel_crawler.alignment import align, catalog, extract_chapter, overlap, title_key
from novel_crawler.config import load
from novel_crawler.multisource import MultiSourceCrawler
from novel_crawler.store import Store
from novel_crawler.runtime import ensure_config, report, backup_daily


def entries(names, start=1):
    return [{'number': i, 'title': f'第{i}章 {name}', 'url': f'https://example.org/{i}'}
            for i, name in enumerate(names, start)]


def test_number_shift_is_matched_by_title_and_neighbors():
    canonical = entries(['归家', '夜话', '赴宴', '风雨', '启程'])
    candidate = entries(['归家', '夜话', '赴宴', '风雨', '启程'], 100)
    assert {i: v[0] for i, v in align(canonical, candidate).items()} == dict(enumerate(range(1, 6)))


def test_missing_chapter_does_not_shift_following_identity():
    canonical = entries(['归家', '夜话', '赴宴', '风雨', '启程'])
    candidate = entries(['归家', '夜话', '风雨', '启程'])
    assert [v[0] for v in align(canonical, candidate).values()] == [1, 2, 4, 5]


def test_repeated_titles_require_two_anchors():
    canonical = entries(['起点', '重逢', '重逢', '终点'])
    candidate = entries(['起点', '重逢', '重逢', '终点'], 70)
    # Without nearby unique neighbor anchors, reject instead of assuming numbering.
    assert not align(canonical, candidate)
    canonical = entries(['起点', '夜话', '重逢', '重逢', '终点', '归家'])
    candidate = entries(['起点', '夜话', '重逢', '重逢', '终点', '归家'], 70)
    assert len(align(canonical, candidate)) == 6


def test_swapped_chapters_and_split_parts_are_not_accepted():
    canonical = entries(['归家', '夜话', '赴宴', '风雨'])
    swapped = entries(['归家', '赴宴', '夜话', '风雨'])
    mapping = align(canonical, swapped)
    assert 1 not in mapping and 2 not in mapping
    split = entries(['归家', '夜话（上）', '夜话（下）', '风雨'])
    assert not align(canonical, split)
    assert title_key('第１章 “夜话”') == title_key('第99章 夜话')


@pytest.fixture
def fixture(tmp_path):
    cfg = load(str(Path(__file__).parents[1] / 'config.example.toml'))
    cfg['database'] = tmp_path / 'test.db'
    cfg['crawler']['min_chapter_characters'] = 5
    book = cfg['books'][0]
    book.update(id='fixture', source_url='https://canonical.test/toc', toc_selector='a', expected_chapters=3)
    book['sources'] = [dict(id=id, source_url=f'https://{id}.test/toc', encoding='utf-8', toc_selector='a',
                            content_selector='article', title_selector='h1') for id in ['primary', 'backup']]
    store = Store(cfg['database'])
    store.register(book)
    names = ['归家', '夜话', '赴宴']
    def toc(source_names, shift=0):
        return book['original_title'] + book['author'] + ''.join(
            f'<a href="/{i + shift}">第{i + shift}章 {name}</a>' for i, name in enumerate(source_names, 1))
    pages = {book['source_url']: toc(names), book['sources'][0]['source_url']: toc(names),
             book['sources'][1]['source_url']: toc(names, 100)}
    bodies = ['他走到院子里面，看见母亲正在收拾农具。',
              '深夜有一队陌生士兵骑马路过城门，守卫立刻拉响警铃。',
              '宴席上众人谈笑风生，掌柜端来一碗刚煮好的面条。']
    for host, shift in [('primary', 0), ('backup', 100)]:
        for i, name in enumerate(names, 1):
            pages[f'https://{host}.test/{i + shift}'] = f'<h1>第{i + shift}章 {name}</h1><article>{bodies[i-1]}</article>'
    crawler = MultiSourceCrawler(store, cfg['crawler'])
    fetched = []
    def fetch(url, encoding):
        fetched.append(url)
        result = pages[url]
        if isinstance(result, Exception):
            raise result
        return result
    crawler.fetch = fetch
    yield store, cfg, book, crawler, pages, fetched
    store.close()


def test_fallback_on_failed_primary_with_shifted_numbers_and_restart(fixture):
    store, cfg, book, crawler, pages, fetched = fixture
    pages['https://primary.test/2'] = '<h1>第2章 夜话</h1><article>短</article>'
    assert crawler.run(book, 2) == 2
    proof = store.db.execute('SELECT * FROM source_provenance WHERE number=2').fetchone()
    assert proof['source_id'] == 'backup' and proof['source_number'] == 102
    assert crawler.run(book, 2) == 1
    before = len(fetched)
    assert crawler.run(book, 3) == 0
    assert len(fetched) == before
    assert fetched.count('https://backup.test/102') == 1
    assert len(store.db.execute('SELECT * FROM source_provenance').fetchall()) == 3


def test_duplicates_and_wrong_page_title_never_overwrite_existing(fixture):
    store, cfg, book, crawler, pages, fetched = fixture
    assert crawler.run(book, 1) == 1
    original = store.db.execute('SELECT source FROM chapters WHERE number=1').fetchone()[0]
    pages['https://primary.test/2'] = '<h1>第2章 夜话</h1><article>' + json.loads(original)[0].replace('，', '！') + '</article>'
    pages['https://backup.test/102'] = '<h1>第103章 赴宴</h1><article>这不是应该翻译的章节内容。</article>'
    assert crawler.run(book, 1) == 0
    assert store.db.execute('SELECT state FROM chapters WHERE number=2').fetchone()[0] == 'source_review'
    assert store.db.execute('SELECT source FROM chapters WHERE number=1').fetchone()[0] == original
    # Review does not prevent further CRAWLING; translation still stops at the gap.
    assert crawler.run(book, 1) == 1
    assert len(store.db.execute('SELECT * FROM source_provenance').fetchall()) == 2


def test_transient_failure_uses_backup_immediately(fixture):
    store, cfg, book, crawler, pages, fetched = fixture
    pages['https://primary.test/1'] = httpx.ReadTimeout('temporary')
    assert crawler.run(book, 1) == 1
    assert store.db.execute('SELECT source_id FROM source_provenance WHERE number=1').fetchone()[0] == 'backup'


def test_primary_catalog_gap_is_checked_before_normal_queue(fixture):
    store, cfg, book, crawler, pages, fetched = fixture
    soup = BeautifulSoup(pages['https://primary.test/toc'], 'html.parser')
    soup.select_one('a[href="/2"]').decompose()
    pages['https://primary.test/toc'] = str(soup)
    assert crawler.run(book, 1) == 1
    assert store.db.execute('SELECT number,source_id FROM source_provenance').fetchone()[:] == (2, 'backup')
    assert store.db.execute('SELECT state FROM chapters WHERE number=1').fetchone()[0] == 'discovered'


def test_transient_retries_have_persistent_bound(fixture):
    store, cfg, book, crawler, pages, fetched = fixture
    pages['https://primary.test/1'] = httpx.ReadTimeout('temporary')
    pages['https://backup.test/101'] = httpx.ReadTimeout('temporary')
    for _ in range(3):
        assert crawler.run(book, 1) == 0
        store.db.execute('UPDATE source_candidates SET retry_at=0')
    assert store.db.execute('SELECT state FROM chapters WHERE number=1').fetchone()[0] == 'source_review'
    assert fetched.count('https://primary.test/1') == 3
    assert fetched.count('https://backup.test/101') == 3


def test_new_source_unblocks_previously_missing_content(fixture):
    store, cfg, book, crawler, pages, fetched = fixture
    for host, number in [('primary', 1), ('backup', 101)]:
        pages[f'https://{host}.test/{number}'] = '<h1>第1章 归家</h1><article>短</article>'
    assert crawler.run(book, 1) == 0
    extra = copy.deepcopy(book['sources'][0])
    extra.update(id='extra', source_url='https://extra.test/toc')
    book['sources'].append(extra)
    pages['https://extra.test/toc'] = pages['https://primary.test/toc']
    pages['https://extra.test/1'] = '<h1>第1章 归家</h1><article>清晨他推开家门，慢慢走向院子里面的水井。</article>'
    assert crawler.run(book, 1) == 1
    assert not store.db.execute('SELECT 1 FROM source_reviews WHERE number=1').fetchone()


def test_wrong_work_declared_truncation_and_pagination_rejected(fixture):
    store, cfg, book, crawler, pages, fetched = fixture
    with pytest.raises(ValueError, match='author'):
        catalog('寒门败家子 作者：另一个人<a href="/1">第1章 归家</a>', book['sources'][0], book)
    source = book['sources'][0]
    with pytest.raises(ValueError, match='declared'):
        extract_chapter('<h1>第1章 归家</h1>本章字数：2000<article>今天他回到了自己家里。</article>', source, '第1章 归家', cfg['crawler'])
    with pytest.raises(ValueError, match='pagination'):
        extract_chapter('<h1>第1章 归家</h1><article>今天他回到了自己家里。</article><a href="/2">下一页</a>', source, '第1章 归家', cfg['crawler'])


def test_backup_and_report_do_not_send_or_call_agents(fixture):
    store, cfg, book, crawler, pages, fetched = fixture
    crawler.run(book, 1)
    backup_daily(store)
    backup_daily(store)
    assert len(list((cfg['database'].parent / 'backups').glob('*.db'))) == 1
    report(store, cfg)
    data = json.loads((cfg['database'].parent / 'status.json').read_text(encoding='utf-8'))
    assert data['calls']['count'] == 0
    assert data['books'][0]['states']['crawled'] == 1


def test_substantial_containment_detects_combined_chapter():
    a = '风吹过院子，母亲回过头来。' * 10
    assert overlap(a, a + '另一章完全不同的内容。' * 30) == 1


def test_piaotia_inline_markup_does_not_truncate_paragraph():
    html = '<H1>寒门败家子 第一章 归家</H1><div class="toplink">广告</div><table><tr><td>广告</td></tr></table>'
    html += '<br><br>&nbsp;&nbsp;成<a href="/other">亲以后</a>他们就住在村里。<br><br>'
    html += '&nbsp;&nbsp;他回到了家里。<br><!-- 翻页上AD开始 -->'
    limits = {'min_chapter_characters': 5, 'max_chapter_characters': 1000}
    assert extract_chapter(html, {'adapter': 'piaotia'}, '第一章 归家', limits) == [
        '成亲以后他们就住在村里。', '他回到了家里。']


def test_piaotia_orphan_tag_repairs_only_artificial_boundary():
    html = '<H1>第一章 归家</H1><br>&nbsp;成<br /><br />&nbsp;</di><br /><br />&nbsp;州的百姓回家了。'
    html += '<br /><br />&nbsp;他推开了大门。<!-- 翻页上AD开始 -->'
    limits = {'min_chapter_characters': 5, 'max_chapter_characters': 1000}
    assert extract_chapter(html, {'adapter': 'piaotia'}, '第一章 归家', limits) == [
        '成州的百姓回家了。', '他推开了大门。']


def test_piaotia_html_line_wrap_does_not_create_extra_paragraph():
    html = '<H1>第一章 归家</H1><br>他早上不\n知怎么回家了。<br>母亲站在门口。<!-- 翻页上AD开始 -->'
    limits = {'min_chapter_characters': 5, 'max_chapter_characters': 1000}
    assert extract_chapter(html, {'adapter': 'piaotia'}, '第一章 归家', limits) == [
        '他早上不 知怎么回家了。', '母亲站在门口。']


def test_one_scheduler_cycle_crawls_fills_translates_edits_exports_and_sends(fixture, monkeypatch):
    from novel_crawler.cli import tick
    from novel_crawler.epub import verify_epub
    store, cfg, book, crawler, pages, fetched = fixture
    cfg['jobs'].update(crawl_batch=3, translate_batch=3, edit_batch=3)
    cfg['limits']['min_call_interval_seconds'] = .001
    cfg['output_dir'] = cfg['database'].parent / 'epub'
    book['glossary'] = cfg['database'].parent / 'glossary.json'
    book['glossary'].write_text('{}', encoding='utf-8')
    cfg['kindle'].update(enabled=True, smtp_host='smtp.test', sender='sender@test.com', recipient='reader@kindle.com')
    monkeypatch.setenv('KINDLE_SMTP_USERNAME', 'test')
    monkeypatch.setenv('KINDLE_SMTP_PASSWORD', 'test')
    pages['https://primary.test/2'] = '<h1>第2章 夜话</h1><article>短</article>'
    class Agent:
        provider, model = 'codex_cli', 'gpt-5.6-terra'
        def __init__(self, *a): pass
        def generate(self, prompt):
            data = json.loads(prompt.split('\nDATA:\n')[1])
            return json.dumps({'title': 'Đêm ở làng', 'paragraphs': [
                {'id': p['id'], 'text': 'Đêm đã khuya, người trong làng đều yên giấc.'} for p in data['source']],
                'continuity': 'Một đêm ở làng.', 'issues': []}, ensure_ascii=False), 100
    class SMTP:
        calls = 0
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, **kw): pass
        def login(self, *a): pass
        def sendmail(self, *a):
            SMTP.calls += 1
            return {}
    monkeypatch.setattr('novel_crawler.cli.MultiSourceCrawler', lambda *a: crawler)
    monkeypatch.setattr('novel_crawler.cli.CliAgent', Agent)
    monkeypatch.setattr('novel_crawler.delivery.smtplib.SMTP', SMTP)
    assert tick(store, cfg, now=100000) == []
    assert SMTP.calls == 1
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 6
    assert verify_epub(cfg['output_dir'] / (book['id'] + '.epub'))
    assert store.db.execute('SELECT source_number FROM source_provenance WHERE number=2').fetchone()[0] == 102
    assert tick(store, cfg, now=200000) == []
    assert SMTP.calls == 1
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 6
