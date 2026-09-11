"""Conservative removal of repeated whole documents, never repeated individual lines."""
import re


def deduplicate_document(paragraphs):
    if len(paragraphs) < 24 or len(paragraphs[0]) < 10:
        return paragraphs, None
    text = ''.join(paragraphs)
    starts = [m.start() for m in re.finditer(re.escape(paragraphs[0]), text)]
    if len(starts) < 2 or starts[0] != 0:
        return paragraphs, None
    ends = starts[1:] + [len(text)]
    parts = [text[a:b] for a, b in zip(starts, ends)]
    # Only an already recognized orphan ?? after sentence punctuation may differ.
    # All meaningful characters, punctuation and their order must be identical.
    normalized = [re.sub(r'(?<=[。！？.!?])\s*\?{2,}(?=[\u4e00-\u9fff]|$)', '', p) for p in parts]
    if len(normalized[0]) < 500 or any(p != normalized[0] for p in normalized[1:]):
        return paragraphs, None
    kept, remaining = [], starts[1]
    for p in paragraphs:
        if remaining <= 0:
            break
        kept.append(p[:remaining])
        remaining -= len(p)
    if len(kept) < 12:
        return paragraphs, None
    # Every retained paragraph must have an exact complete counterpart in the
    # original, including a last paragraph glued to the repeated opening line.
    indexes = {}
    for i, p in enumerate(paragraphs, 1):
        indexes.setdefault(p, i)
    if any(p not in indexes for p in kept):
        return paragraphs, None
    return kept, {'copies': len(parts), 'retained_paragraph_ids': [indexes[p] for p in kept]}
