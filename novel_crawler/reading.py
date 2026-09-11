"""Reader paragraph boundaries are independent of source alignment IDs."""
import re
import unicodedata


def normalize_text(text):
    text = unicodedata.normalize('NFC', text).replace('\r\n', '\n').replace('\r', '\n')
    # One newline is an accidental wrap; a blank line is an intentional break.
    return '\n\n'.join(re.sub(r'\s+', ' ', part).strip()
                       for part in re.split(r'\n\s*\n', text) if part.strip())


def alignment_groups(source, paragraphs):
    """Only explicit joins across unfinished source sentences share validation.

    A layout flag alone cannot borrow a name from a different complete sentence.
    Indices and original source strings remain intact for contextual evidence.
    """
    groups = []
    for index, paragraph in enumerate(paragraphs):
        previous = source[index - 1].rstrip() if index else ''
        ended = re.search(r'''[。！？!?；;.…:：][”’"'）)\]】]*$''', previous)
        if (groups and paragraph.get('join_previous') and previous and not ended
                and '\n\n' not in source[index - 1]):
            groups[-1].append(index)
        else:
            groups.append([index])
    return groups


def reading_paragraphs(paragraphs):
    result = []
    for paragraph in paragraphs:
        text = normalize_text(paragraph['text'])
        # Adjacent complete quotations are separate utterances, not one block.
        text = re.sub(r'([.!?…]”)\s*(?=“)', r'\1\n\n', text)
        chunks = text.split('\n\n')
        for index, chunk in enumerate(chunks):
            if not chunk:
                continue
            if index == 0 and paragraph.get('join_previous') and result:
                result[-1] += ' ' + chunk
            else:
                result.append(chunk)
    return result
