import hashlib
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup


def chapter_number(title):
    title = unicodedata.normalize('NFKC', title)
    match = re.search(r'第([0-9零〇一二两三四五六七八九十百千万]+)章', title)
    if not match:
        raise ValueError(f'Cannot parse chapter number: {title}')
    value = match[1]
    if value.isascii() and value.isdigit():
        return int(value)
    digits = dict(zip('零〇一二两三四五六七八九', [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    units = {'十': 10, '百': 100, '千': 1000}
    total = section = digit = 0
    for char in value:
        if char in digits:
            digit = digits[char]
        elif char == '万':
            total += (section + digit) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * units[char]
            digit = 0
    return total + section + digit


def parse_toc(html, book):
    soup = BeautifulSoup(html, 'html.parser')
    chapters = {}
    for a in soup.select(book['toc_selector']):
        title = a.get_text(' ', strip=True)
        if '章' not in title:
            continue
        number = chapter_number(title)
        url = urljoin(book['source_url'], a.get('href', ''))
        if urlsplit(url).netloc != urlsplit(book['source_url']).netloc:
            raise ValueError('Cross-host chapter URL rejected')
        if number in chapters and chapters[number]['url'] != url:
            raise ValueError('Conflicting chapter URLs')
        chapters[number] = {'number': number, 'title': title.removeprefix('最近更新：'), 'url': url}
    expected = set(range(1, book['expected_chapters'] + 1))
    if set(chapters) != expected:
        raise ValueError(f'TOC incomplete: missing {sorted(expected - chapters.keys())[:30]}, '
                         f'extra {sorted(chapters.keys() - expected)[:10]}')
    return [chapters[n] for n in sorted(chapters)]


def parse_content(html, selector, limits):
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.select_one(selector)
    if node is None:
        raise ValueError('Content selector missing; source layout changed')
    for element in node.select('script,style,nav,aside,button'):
        element.decompose()
    paragraphs = [re.sub(r'[\t \u3000]+', ' ', line).strip()
                  for line in node.get_text('\n').splitlines()]
    paragraphs = [p for p in paragraphs if p]
    text = '\n'.join(paragraphs)
    if not limits['min_chapter_characters'] <= len(text) <= limits['max_chapter_characters']:
        raise ValueError('Chapter length outside bounds; possibly truncated or app-only')
    if re.search(r'[\ue000-\uf8ff\ufffd]', text):
        raise ValueError('Obfuscated font characters; source needs a verified adapter')
    if len(re.findall(r'[\u4e00-\u9fff]', text)) / max(len(text), 1) < .4:
        raise ValueError('Source has too little readable Chinese')
    if any(marker in text for marker in ('下载APP继续阅读', '请登录后阅读', '购买本章', '验证码')):
        raise ValueError('Access interstitial found in content')
    return paragraphs


class Crawler:
    def __init__(self, store, config):
        self.store = store
        self.cfg = dict(config, user_agent=config.get('user_agent') or f'python-httpx/{httpx.__version__}')
        self.last_request = 0.0
        self.robots = {}

    def _get(self, url):
        delay = self.cfg['request_interval_seconds'] - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        with httpx.Client(timeout=self.cfg['timeout_seconds'], follow_redirects=False,
                          headers={'User-Agent': self.cfg['user_agent']}) as client:
            with client.stream('GET', url) as response:
                if response.status_code == 404:
                    return None
                response.raise_for_status() # Includes redirects; no silent host change
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > self.cfg['max_response_bytes']:
                        raise ValueError('HTTP response exceeds size limit')
                return bytes(data)

    def fetch(self, url, encoding):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValueError('Only HTTP(S) sources are supported')
        origin = f'{parts.scheme}://{parts.netloc}'
        if origin not in self.robots:
            raw = self._get(origin + '/robots.txt')
            parser = RobotFileParser()
            parser.parse(raw.decode('utf-8', errors='replace').splitlines() if raw else [])
            self.robots[origin] = parser
        parser = self.robots[origin]
        if not parser.can_fetch(self.cfg['user_agent'], url):
            raise ValueError('robots.txt disallows this URL')
        delay = parser.crawl_delay(self.cfg['user_agent']) or parser.crawl_delay('*') or 0
        if delay > self.cfg['request_interval_seconds']:
            self.cfg = dict(self.cfg, request_interval_seconds=delay)
        raw = self._get(url)
        if raw is None:
            raise ValueError('Source returned 404')
        return raw.decode(encoding) # Fail on broken encoding rather than translating garbage

    def run(self, book, batch):
        db = self.store.db
        ready = db.execute('SELECT toc_ready FROM books WHERE id=?', (book['id'],)).fetchone()[0]
        if not ready:
            chapters = parse_toc(self.fetch(book['source_url'], book['encoding']), book)
            with self.store.transaction():
                for ch in chapters:
                    db.execute('INSERT OR IGNORE INTO chapters(book_id,number,url,title) VALUES(?,?,?,?)',
                               (book['id'], ch['number'], ch['url'], ch['title']))
                db.execute('UPDATE books SET toc_ready=1 WHERE id=?', (book['id'],))
        done = 0
        for _ in range(batch):
            ch = db.execute('SELECT * FROM chapters WHERE book_id=? AND source IS NULL ORDER BY number LIMIT 1',
                            (book['id'],)).fetchone()
            if ch is None or ch['state'] != 'discovered':
                break
            try:
                text = parse_content(self.fetch(ch['url'], book['encoding']), book['content_selector'], self.cfg)
                digest = hashlib.sha256('\n'.join(text).encode()).hexdigest()
                duplicate = db.execute('SELECT number FROM chapters WHERE book_id=? AND source_hash=?',
                                       (book['id'], digest)).fetchone()
                if duplicate:
                    raise ValueError(f'Duplicate source content from chapter {duplicate[0]}')
                db.execute("UPDATE chapters SET source=?,source_hash=?,state='crawled',error=NULL WHERE book_id=? AND number=?",
                           (json.dumps(text, ensure_ascii=False), digest, book['id'], ch['number']))
                done += 1
            except Exception as exc:
                db.execute("UPDATE chapters SET state='crawl_failed',error=? WHERE book_id=? AND number=?",
                           (str(exc)[:500], book['id'], ch['number']))
                raise
        return done
