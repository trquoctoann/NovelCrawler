from novel_crawler.source_cleanup import deduplicate_document


def document():
    return [f'第{i}段的故事开始。' + '他走进院子，轻轻推开房门。' * 5 for i in range(14)]


def test_whole_document_repetition_with_glued_boundary():
    original = document()
    repeated = original[:-1] + [original[-1] + original[0]] + original[1:]
    clean, proof = deduplicate_document(repeated)
    assert clean == original
    assert proof['copies'] == 2
    assert proof['retained_paragraph_ids'] == list(range(1, 14)) + [27]
    assert all(repeated[i - 1] == p for i, p in zip(proof['retained_paragraph_ids'], clean))


def test_only_orphan_question_marks_may_differ():
    original = document()
    first = original.copy()
    first[3] = first[3].replace('。他', '。 ??他', 1)
    clean, proof = deduplicate_document(first + original)
    assert clean == first and proof['copies'] == 2
    other = original.copy(); other[3] += '她离开了。'
    repeated = first + other
    assert deduplicate_document(repeated) == (repeated, None)


def test_individual_refrains_and_nonidentical_copies_are_preserved():
    original = document()
    assert deduplicate_document(original) == (original, None)
    with_refrains = [p for line in original for p in [line, '他点了点头。']]
    assert deduplicate_document(with_refrains) == (with_refrains, None)
    other = original.copy(); other[-1] = other[-1].replace('轻轻', '缓缓', 1)
    combined = original + other
    assert deduplicate_document(combined) == (combined, None)
