"""Conservative chapter identity matching, independent of a site's numbering."""
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import re
import unicodedata
from urllib.parse import urljoin, urlsplit, parse_qs

from bs4 import BeautifulSoup

from .crawler import chapter_number, parse_content


def normalized(text):
    text = unicodedata.normalize('NFKC', text)
    return ''.join(c.lower() for c in text if c.isalnum())


def title_key(title):
    # Keep 上/下/续/part markers: stripping them would silently merge split chapters.
    title = unicodedata.normalize('NFKC', title)
    title = re.sub(r'^.*?第[0-9零〇一二两三四五六七八九十百千万]+章\s*', '', title)
    return normalized(title)


def matching_key(title, literal=False):
    # Number-only chapter titles have no subtitle to strip. Preserve the entire
    # label in an explicitly pinned edition, including prologues and extras.
    return normalized(title) if literal else title_key(title)


def catalog(html, source, book):
    soup = BeautifulSoup(html, 'html.parser')
    # Metadata must be present on the catalog itself, not supplied by the caller.
    visible = normalized(soup.get_text(' ', strip=True))
    if normalized(book['original_title']) not in visible or normalized(book['author']) not in visible:
        raise ValueError('Catalog does not identify the expected book and author')
    entries, seen = [], {}
    ordered = source.get('toc_mode') == 'ordered'
    jjwxc = source.get('toc_mode') == 'jjwxc'
    literal = book.get('literal_titles', False)
    for a in soup.select(source['toc_selector']):
        title = unicodedata.normalize('NFKC', a.get_text(' ', strip=True)).removeprefix('最近更新:')
        if not (ordered or jjwxc) and not re.search(r'第[0-9零〇一二两三四五六七八九十百千万]+章', title):
            continue
        url = urljoin(source['source_url'], a.get('href', ''))
        if urlsplit(url).netloc != urlsplit(source['source_url']).netloc:
            raise ValueError('Cross-host chapter link')
        if jjwxc:
            query = parse_qs(urlsplit(url).query)
            expected_book = parse_qs(urlsplit(source['source_url']).query).get('novelid')
            row = a.find_parent('tr')
            number = int(query.get('chapterid', ['0'])[0])
            if (query.get('novelid') != expected_book or number < 1 or row is None
                    or row.select_one('td').get_text(strip=True) != str(number)):
                raise ValueError('JJWXC chapter link disagrees with its catalog row')
            # The public site publishes HTTP links; HTTPS is supported on the
            # same host. Never follow VIP purchase links or use another host.
            url = url.replace('http://', 'https://', 1)
        else:
            number = len(entries) + 1 if ordered else chapter_number(title)
        key = matching_key(title, literal)
        if not key:
            continue
        if url in seen:
            if seen[url] != key:
                raise ValueError('Same chapter URL has conflicting titles')
            continue
        seen[url] = key
        entries.append({'number': number, 'title': title, 'key': key, 'url': url})
    if not entries:
        raise ValueError('Empty source catalog')
    # A latest-chapter banner can precede the main catalog. Sort only when numeric
    # labels are unique; mappings below never equate these numbers to canonical IDs.
    if len({e['number'] for e in entries}) == len(entries):
        entries.sort(key=lambda e: e['number'])
    return entries


def align(canonical, candidate, literal=False):
    """Return index -> (canonical number, evidence); ambiguous identities stay out."""
    key_for = lambda title: matching_key(title, literal)
    canonical_counts = Counter(key_for(e['title']) for e in canonical)
    source_counts = Counter(key_for(e['title']) for e in candidate)
    unique = {key_for(e['title']): e['number'] for e in canonical
              if canonical_counts[key_for(e['title'])] == 1}
    anchors = {i: unique[key] for i, e in enumerate(candidate)
               if (key := key_for(e['title'])) in unique and source_counts[key] == 1}
    result = {}
    for i, number in anchors.items():
        neighbors = [(j, anchors[j]) for j in (i - 1, i + 1) if j in anchors]
        if not neighbors:
            continue
        # Require ALL immediate matched neighbors to preserve direction, and one
        # close canonical neighbor. A swapped/injected chapter must not pass.
        ordered = all((j < i and other < number) or (j > i and other > number) for j, other in neighbors)
        close = any(abs(other - number) <= 2 for _, other in neighbors)
        if ordered and close:
            result[i] = (number, 'unique-title+ordered-neighbor')
    # Repeated titles only resolve inside two unique anchors with an exact,
    # gapless title sequence. No fuzzy title or number-only acceptance.
    pairs = sorted(result)
    by_number = {e['number']: key_for(e['title']) for e in canonical}
    for left, right in zip(pairs, pairs[1:]):
        low, high = result[left][0], result[right][0]
        if right - left != high - low or right - left < 2:
            continue
        if all(key_for(candidate[i]['title']) == by_number.get(low + i - left)
               for i in range(left + 1, right)):
            for i in range(left + 1, right):
                result[i] = (low + i - left, 'title-sequence+two-anchors')
    return result


def body_fingerprint(paragraphs):
    text = normalized(''.join(paragraphs))
    return text, hashlib.sha256(text.encode('utf-8')).hexdigest()


def overlap(a, b):
    """High normalized character-shingle containment detects duplicates and merges."""
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    if min(len(a), len(b)) < 30:
        return SequenceMatcher(None, a, b, autojunk=False).ratio()
    left = {a[i:i + 12] for i in range(len(a) - 11)}
    right = {b[i:i + 12] for i in range(len(b) - 11)}
    return len(left & right) / max(1, min(len(left), len(right)))


def extract_chapter(html, source, expected_title, limits):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one(source.get('title_selector', 'h1'))
    key = lambda title: matching_key(title, source.get('literal_titles', False))
    actual = heading.get_text(' ', strip=True).removeprefix(source.get('title_prefix', '')) if heading else ''
    if heading is None or key(actual) != key(expected_title):
        raise ValueError('Page title differs from mapped catalog chapter')
    if any(a.get_text(strip=True) in ('下一页', '下页', '继续阅读本章') for a in soup.select('a[href]')):
        raise ValueError('Split chapter needs an explicit pagination adapter')
    if source.get('adapter') == 'piaotia':
        # This public reader has no content div. Indented lines between ad blocks
        # are the chapter; selectors alone would include navigation/advertising.
        start = html.find('</H1>')
        if start < 0:
            raise ValueError('Piaotia heading boundary changed')
        end = html.find('<!-- 翻页上AD开始 -->', start)
        if end < 0:
            raise ValueError('Piaotia end boundary changed')
        # Split at HTML line breaks BEFORE parsing: the malformed legacy markup
        # nests BR nodes, while inline links/styles may divide a sentence's text.
        body = html[start:end]
        # A known orphan </di> artifact splits words with two artificial BR runs.
        # Join only this exact boundary, retaining every source character. Verified
        # against Shuqi's public chapter 1 (成州); ordinary paragraphs stay separate.
        gap = r'(?:\s|&nbsp;|<br\s*/?>)+'
        body = re.sub(gap + r'</di>' + gap, '', body, flags=re.I)
        # Literal HTML source wrapping is whitespace, not a paragraph boundary.
        # Only BR tags below define reader paragraph breaks for this adapter.
        body = re.sub(r'[\r\n]+', ' ', body)
        fragment = BeautifulSoup(re.sub(r'</?br\s*/?>', '\n', body, flags=re.I), 'html.parser')
        for node in fragment.select('script,style,table,div'):
            node.decompose()
        lines = [line.strip() for line in fragment.get_text().splitlines() if line.strip()]
        if not lines:
            raise ValueError('Piaotia paragraph boundaries changed')
        from html import escape
        html = '<article>' + ''.join('<p>' + escape(p) + '</p>' for p in lines) + '</article>'
        selector = 'article'
    else:
        selector = source['content_selector']
    paragraphs = parse_content(html, selector, limits)
    if source.get('strip_author_notes', False):
        for index, paragraph in enumerate(paragraphs):
            if re.fullmatch(r'作者有话要说[：:]?', paragraph):
                paragraphs = paragraphs[:index]
                break
        if len(''.join(paragraphs)) < limits['min_chapter_characters']:
            raise ValueError('Body is too short after separating author notes')
    declared = re.search(r'本章字数[：:]\s*([0-9,]+)', soup.get_text())
    if declared:
        expected = int(declared[1].replace(',', ''))
        if len(''.join(paragraphs)) < expected * .85:
            raise ValueError('Body is shorter than declared chapter length')
    if sum(bool(re.match(r'^第[0-9零〇一二两三四五六七八九十百千万]+章', p)) for p in paragraphs):
        raise ValueError('Embedded chapter heading suggests merged chapters')
    return paragraphs
