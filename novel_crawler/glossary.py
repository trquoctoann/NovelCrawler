"""Source-aware terminology matching, separate from literary vocabulary guidance.

No fuzzy name matching: accents and the proper-name stem remain significant.
Geographic grammar can move a type word or expand 郡城 without changing identity.
"""
import re
import unicodedata
from functools import lru_cache
import logging

import jieba


jieba.setLogLevel(logging.WARNING)
_tokenizer = jieba.Tokenizer()


@lru_cache(maxsize=2048)
def lexical_spans(source):
    # Dictionary-only segmentation is deterministic. Do not inject glossary
    # aliases here: doing so would force the very false matches being checked.
    return tuple(_tokenizer.tokenize(source, HMM=False))


@lru_cache(maxsize=128)
def mention_pattern(keys):
    return re.compile('|'.join(re.escape(k) for k in sorted(keys, key=lambda k: (-len(k), k))))


PLACE_TYPES = {'郡': ('quận',), '县': ('huyện',), '州': ('châu',),
               '乡': ('hương',), '村': ('thôn', 'làng'), '庄': ('trang', 'thôn'),
               '城': ('thành',), '寨': ('trại',), '镇': ('trấn',)}

# A family name followed by a title can become "Lưu đại thiếu gia".
# These alternatives require that exact title in the source, never a bare surname.
FAMILY_TITLES = {'少爷': 'thiếu gia', '小姐': 'tiểu thư', '老爷': 'lão gia',
                 '公子': 'công tử', '夫人': 'phu nhân'}
for _prefix, _vi in [('大', 'đại'), ('二', 'nhị'), ('三', 'tam'), ('四', 'tứ'),
                     ('五', 'ngũ'), ('六', 'lục'), ('七', 'thất'), ('八', 'bát'), ('九', 'cửu')]:
    for _title in ('少爷', '小姐', '老爷', '公子'):
        FAMILY_TITLES[_prefix + _title] = _vi + ' ' + FAMILY_TITLES[_title]


def normalize(text):
    return ' '.join(unicodedata.normalize('NFC', text).casefold().split())


def contains(text, phrase):
    return re.search(r'(?<!\w)' + re.escape(normalize(phrase)) + r'(?!\w)', normalize(text)) is not None


def source_mentions(source, glossary):
    """Longest known names, with lexical boundaries for short names/surnames.

    Only a short match crossing TWO multi-character words is safely excluded.
    One boundary or a containing word is ambiguous: the tokenizer may be wrong
    about fictional names, so those matches still require validation/review.
    """
    if not glossary:
        return []
    candidates = list(mention_pattern(tuple(sorted(glossary))).finditer(source))
    if not any(len(m.group()) <= 2 for m in candidates):
        return candidates
    tokens = lexical_spans(source)
    def crosses_words(mention):
        overlapping = [(word, start, end) for word, start, end in tokens
                       if start < mention.end() and end > mention.start()]
        return (len(overlapping) >= 2 and all(len(word) >= 2 for word, _, _ in overlapping)
                and overlapping[0][1] < mention.start() and overlapping[-1][2] > mention.end())
    return [m for m in candidates if len(m.group()) > 2 or not crosses_words(m)]


def ordinary_reading_allowed(source, mention):
    """Eligibility to REQUEST a contextual reading, never an automatic waiver."""
    zh = mention.group()
    if len(zh) != 2 or zh[-1] in PLACE_TYPES or zh.endswith('家'):
        return False
    tokens = lexical_spans(source) # Initialize the pinned dictionary first.
    if _tokenizer.FREQ.get(zh, 0) > 0:
        return True # Short dictionary words may also be nicknames (大海/无忧).
    # An ordinary phrase such as 马/前往 can resemble a two-character name.
    return (any(start == mention.start() for _, start, _ in tokens)
            and any(start < mention.end() < end for _, start, end in tokens))


def renderings(zh, vi, source=None, end=None):
    yield vi
    if zh.endswith('家'):
        name = normalize(vi)
        for prefix, suffix in [('nhà họ ', ''), ('nhà ', ''), ('', ' gia')]:
            if name.startswith(prefix) and name.endswith(suffix) and len(name) > len(prefix) + len(suffix):
                stem = name[len(prefix):len(name) - len(suffix) if suffix else None]
                yield 'nhà họ ' + stem
                yield 'nhà ' + stem
                yield stem + ' gia'
                if source is not None and end is not None:
                    for title in sorted(FAMILY_TITLES, key=len, reverse=True):
                        if source.startswith(title, end):
                            yield stem + ' ' + FAMILY_TITLES[title]
                            break
                break
    types = PLACE_TYPES.get(zh[-1:])
    if not types:
        return
    name = normalize(vi)
    stem = None
    for kind in types:
        if name.startswith(kind + ' '):
            stem = name[len(kind) + 1:]
        elif name.endswith(' ' + kind):
            stem = name[:-len(kind) - 1]
        if stem:
            break
    if not stem:
        return
    for kind in types:
        yield kind + ' ' + stem
        yield stem + ' ' + kind
        # Only allow the extra city word when it is actually present in Chinese.
        if source is not None and end is not None and source[end:end + 1] == '城' and zh[-1] != '城':
            yield kind + ' thành ' + stem
            yield stem + ' ' + kind + ' thành'


def matches(zh, vi, text, source=None, end=None):
    return any(contains(text, candidate) for candidate in renderings(zh, vi, source, end))


def equivalent(zh, canonical, proposal):
    return normalize(proposal) in {normalize(v) for v in renderings(zh, canonical)}


def normalize_aliases(source, text, glossary, aliases):
    """Canonicalize only aliases that cannot belong to another source mention.

    Paragraph-level Chinese/VI alignment cannot tell which occurrence a shared
    alias refers to. Preserve it for normal validation instead of guessing.
    """
    active = {}
    for mention in source_mentions(source, glossary):
        zh = mention.group()
        active.setdefault(zh, set()).update(renderings(zh, glossary[zh], source, mention.end()))
        active[zh].update(aliases.get(zh, []))
    replacements = {}
    for zh, variants in aliases.items():
        if zh not in active:
            continue
        for variant in variants:
            if any(contains(rendering, variant) for other, values in active.items()
                   if other != zh for rendering in values):
                continue
            replacements.setdefault(normalize(variant), set()).add(glossary[zh])
    replacements = {variant: next(iter(values)) for variant, values in replacements.items() if len(values) == 1}
    if not replacements:
        return text
    pattern = r'(?<!\w)(?:' + '|'.join(r'\s+'.join(re.escape(word) for word in variant.split())
        for variant in sorted(replacements, key=len, reverse=True)) + r')(?!\w)'
    # A single pass prevents a replacement from being rewritten as another alias.
    return re.sub(pattern, lambda match: replacements[normalize(match.group())], text, flags=re.IGNORECASE)


def requirements(source, glossary):
    return [{'source': m.group(), 'translation': glossary[m.group()],
             'source_start': m.start(), 'source_end': m.end(),
             'ordinary_reading_allowed': ordinary_reading_allowed(source, m),
             'accepted_renderings': list(dict.fromkeys(renderings(m.group(), glossary[m.group()], source, m.end())))}
            for m in source_mentions(source, glossary)]


def mismatches(source, text, glossary, ordinary_starts=()):
    return [f'{m.group()} -> {glossary[m.group()]}' for m in source_mentions(source, glossary)
            if m.start() not in ordinary_starts and not matches(m.group(), glossary[m.group()], text, source, m.end())]
