"""Conservative comparison of Arabic literals and simple Chinese integers."""
import re

from .crawler import chapter_number


LITERAL = r'\d+(?:[.,]\d+)*'
HAN = '零一二三四五六七八九'


def canonical_han(value):
    if not 0 <= value < 10**12:
        return None
    for scale, unit in ((10**8, '亿'), (10000, '万')):
        if value >= scale:
            high, low = divmod(value, scale)
            return (canonical_han(high) + unit +
                    (('零' if low < scale // 10 else '') + canonical_han(low) if low else ''))
    if value == 0:
        return HAN[0]
    output = ''
    zero = False
    for divisor, unit in ((1000, '千'), (100, '百'), (10, '十'), (1, '')):
        digit, value = divmod(value, divisor)
        if digit:
            if zero:
                output += '零'
            if not (divisor == 10 and digit == 1 and not output):
                output += HAN[digit]
            output += unit
            zero = False
        elif output and value:
            zero = True
    return output


def han_value(token):
    if not token:
        return -1
    if '亿' in token:
        if token.count('亿') != 1:
            return -1
        high, low = token.split('亿')
        return han_value(high) * 10**8 + (han_value(low) if low else 0)
    return chapter_number('第' + token + '章')


def literal_scales(text, vietnamese=False):
    scales = []
    for match in re.finditer(LITERAL, text):
        tail = text[match.end():]
        if vietnamese:
            unit = re.match(r'\s*(vạn|nghìn|ngàn|triệu|tỷ)\b', tail, re.IGNORECASE)
            scale = {'vạn': 10000, 'nghìn': 1000, 'ngàn': 1000, 'triệu': 10**6, 'tỷ': 10**9}.get(unit[1].lower(), 1) if unit else 1
        else:
            scale = {'万': 10000, '亿': 10**8}.get(tail[:1], 1)
        scales.append(scale)
    return scales


def numeric_literals_match(source, translated):
    original = re.findall(LITERAL, source)
    actual = re.findall(LITERAL, translated)
    scales = literal_scales(translated, vietnamese=True)
    if original == actual:
        return literal_scales(source) == scales
    # Never relax existing Arabic literals, decimals, grouping or leading zeros.
    integers = []
    for token in actual:
        if token.isascii() and token.isdigit() and str(int(token)) == token:
            integers.append(int(token))
        elif re.fullmatch(r'[1-9][0-9]{0,2}(?:\.[0-9]{3})+', token):
            integers.append(int(token.replace('.', ''))) # Vietnamese thousands separators
        else:
            return False
    if original:
        return False
    values = []
    for match in re.finditer(r'[零〇一二两三四五六七八九十百千万亿]+', source):
        token = match.group()
        # 两 is also the unit liang (tael). After a complete number it is
        # a unit, not the digit two: 五十两银子 = 50 taels, not 52.
        # Keep 两百 = 200 and ambiguous 两三/一两个人 unchanged.
        if len(token) > 1 and token.endswith('两'):
            prefix = token[:-1].replace('〇', '零').replace('两', '二')
            amount = han_value(prefix)
            canonical = canonical_han(amount)
            money_context = re.match(r'(?:白银|黄金|银|金|钱)', source[match.end():])
            if (prefix == canonical and (amount >= 3 or money_context or token[:-1] == '二')):
                token = token[:-1]
        normalized = token.replace('〇', '零').replace('两', '二')
        value = han_value(normalized)
        canonical = canonical_han(value)
        if normalized == canonical or (canonical and canonical.startswith('十') and normalized == '一' + canonical):
            values.append(str(value))
    # A converted numeral must occur in the source, with the same multiplicity
    # and order. Ambiguous expressions such as 两三/三四十 are not exact numbers.
    remaining = iter(values)
    return all(any(value == str(value_int * scale) for value in remaining) for value_int, scale in zip(integers, scales))
