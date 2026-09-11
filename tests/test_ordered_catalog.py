import json
from pathlib import Path
import pytest
from novel_crawler.alignment import catalog, align, extract_chapter
from novel_crawler.crawler import parse_toc
from novel_crawler.completion import finalize_missing, is_complete
from novel_crawler.config import load
from test_multisource import fixture


def ordered_book():
    titles = ['第1章 楔子', '第2章', '第3章', '番外一 归家']
    ref = [dict(number=i, source_title=t, url=f'https://books.test/{i}',word_count=500)
           for i,t in enumerate(titles,1)]
    book = dict(id='ordered',original_title='长风渡',author='墨书白',source_url='https://books.test/',
                toc_selector='ul a',toc_mode='ordered',literal_titles=True,expected_chapters=4,
                _reference_entries=ref,content_selector='article',title_prefix='长风渡 正文 ')
    html = '长风渡 墨书白<ul>'+''.join(f'<li><a href="/{i}">{t}</a></li>' for i,t in enumerate(titles,1))+'</ul>'
    return book, html


def test_ordered_catalog_preserves_number_only_titles_prologue_and_extra():
    book, html = ordered_book()
    entries = parse_toc(html,book)
    assert len(entries) == 4 and entries[-1]['title'] == '番外一 归家'
    assert len(align(entries, catalog(html,book,book),literal=True)) == 4


@pytest.mark.parametrize('old,new', [('/2','/other'), ('第2章','第8章'), ('墨书白','其他作者')])
def test_ordered_catalog_rejects_edition_drift(old,new):
    book, html = ordered_book()
    with pytest.raises(ValueError):parse_toc(html.replace(old,new),book)


def test_unpinned_ordered_catalog_is_rejected():
    book, html = ordered_book();book.pop('_reference_entries')
    with pytest.raises(ValueError,match='pinned'):parse_toc(html,book)


def test_literal_titles_do_not_collapse_distinct_chapters_or_extras():
    book, html = ordered_book();entries=parse_toc(html,book)
    other=[dict(e) for e in entries];other[1]['title']='第99章'
    assert 1 not in align(entries,other,literal=True)
    swapped=[entries[0],entries[2],entries[1],entries[3]]
    result=align(entries,swapped,literal=True)
    assert 1 not in result and 2 not in result


def test_literal_page_title_and_author_notes_are_checked():
    book,_=ordered_book();source=dict(book,strip_author_notes=True)
    limits=dict(min_chapter_characters=5,max_chapter_characters=30000)
    body='<article><p>院子里面的桃花开了，大家都非常开心。</p><p>作者有话要说：</p><p>订阅其他小说吧。</p></article>'
    result=extract_chapter('<h1>长风渡 正文 第2章</h1>'+body,source,'第2章',limits)
    assert len(result)==1 and '订阅' not in result[0]
    with pytest.raises(ValueError,match='title'):extract_chapter('<h1>长风渡 正文 第3章</h1>'+body,source,'第2章',limits)


def test_reference_length_rejects_truncated_body(fixture):
    store,cfg,book,crawler,pages,fetched=fixture
    book['_reference_entries']=[dict(word_count=1000) for _ in range(3)]
    assert crawler.run(book,1)==0
    assert store.db.execute('SELECT source,state FROM chapters WHERE number=1').fetchone()[:]==(None,'source_review')


def test_complete_book_policy_does_not_finalize_missing_or_export(fixture):
    store,cfg,book,crawler,pages,fetched=fixture;crawler.run(book,3)
    book['allow_missing_chapters']=False
    store.db.execute("UPDATE chapters SET source=NULL,state='source_review'")
    assert finalize_missing(store,book)==0
    store.db.execute("UPDATE chapters SET state='missing',summary='Unavailable'")
    assert not is_complete(store,book)


def test_config_loads_pinned_new_book(tmp_path):
    root=Path(__file__).parents[1]
    base=(root/'config.example.toml').read_text(encoding='utf-8').split('[[books]]',1)[0]
    block=(root/'books/chang-feng-du/book.toml').read_text(encoding='utf-8')
    block=block.replace('books/chang-feng-du/',(root/'books/chang-feng-du').as_posix()+'/')
    path=tmp_path/'config.toml';path.write_text(base+block,encoding='utf-8')
    cfg=load(str(path));book=cfg['books'][0]
    assert len(book['_reference_entries'])==176
    assert book['sources'][0]['literal_titles'] is True
    assert book['allow_missing_chapters'] is False


def test_jjwxc_reference_titles_fill_primary_gap_without_number_guessing(fixture):
    from bs4 import BeautifulSoup
    store,cfg,book,crawler,pages,fetched=fixture
    book['literal_titles']=True
    original_titles=['楔子','第一章','第二章']
    bodies=[BeautifulSoup(pages[f'https://primary.test/{n}'],'html.parser').article.text for n in range(1,4)]
    book['_reference_entries']=[dict(number=n,original_title=t,word_count=len(bodies[n-1]))
                               for n,t in enumerate(original_titles,1)]
    source=dict(id='official',source_url='https://official.test/toc?novelid=123',toc_selector='tr a[href]',
                toc_mode='jjwxc',reference_titles=True,literal_titles=True,encoding='utf-8',
                content_selector='article',title_selector='h2')
    book['sources'].append(source)
    rows=[]
    for n,title in enumerate(original_titles,1):
        rows.append(f'<tr><td>{n}</td><td><a href="https://official.test/read?novelid=123&amp;chapterid={n}">{title}</a></td></tr>')
        pages[f'https://official.test/read?novelid=123&chapterid={n}']=f'<h2>{title}</h2><article>{bodies[n-1]}</article>'
    pages[source['source_url']]=book['original_title']+book['author']+'<table>'+''.join(rows)+'</table>'
    pages['https://primary.test/1']='<h1>第1章 归家</h1><article>短</article>'
    pages['https://backup.test/101']='<h1>wrong</h1>'
    assert crawler.run(book,1)==1
    provenance=store.db.execute('SELECT source_id,title FROM source_provenance WHERE number=1').fetchone()
    assert tuple(provenance)==('official','楔子')
    assert not any('vip' in url for url in fetched)


def test_jjwxc_catalog_rejects_wrong_book_query_and_row_id():
    book,_=ordered_book()
    source=dict(source_url='https://books.test/toc?novelid=123',toc_selector='tr a[href]',toc_mode='jjwxc')
    for query,row in [('novelid=999&chapterid=1',1),('novelid=123&chapterid=2',1)]:
        html=f'长风渡 墨书白<table><tr><td>{row}</td><td><a href="/read?{query}">楔子</a></td></tr></table>'
        with pytest.raises(ValueError,match='JJWXC'):catalog(html,source,book)


def test_html_encoding_tolerance_still_rejects_damaged_body():
    from novel_crawler.crawler import Crawler,parse_content
    c=Crawler(None,dict(user_agent='test',request_interval_seconds=1))
    c._get=lambda url: b'' if url.endswith('/robots.txt') else '<article>院子里面'.encode('gb18030')+b'\xff'+ '</article>'.encode('gb18030')
    html=c.fetch('https://books.test/chapter','gb18030-html')
    with pytest.raises(ValueError,match='Obfuscated'):parse_content(html,'article',dict(min_chapter_characters=1,max_chapter_characters=100))
