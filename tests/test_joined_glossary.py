import json

import pytest

from novel_crawler.translation import validate_result, editing_draft, GlossaryReviewError, EditingReviewError


def result(texts, joins=None):
    joins = joins or [False] + [True] * (len(texts) - 1)
    return {'title': 'Dân làng', 'continuity': 'Dân làng nhìn người bị trói.', 'issues': [],
            'paragraphs': [{'id': i, 'text': text, 'join_previous': join}
                           for i, (text, join) in enumerate(zip(texts, joins), 1)]}


@pytest.mark.parametrize('draft', [False, True])
def test_chapter_62_name_moves_within_joined_sentence(draft):
    source = ['许多村民围着被绑', '的黑心虎，远远看着议论纷纷！']
    data = result(['Nhiều dân làng vây quanh Hắc Tâm Hổ bị trói',
                   'trên xe, đứng từ xa nhìn rồi bàn tán không ngớt!'])
    data['terms'] = [{'source': '黑心虎', 'translation': 'Hắc Tâm Hổ'}]
    accepted = validate_result(json.dumps(data), source, {'黑心虎': 'Hắc Tâm Hổ'}, allow_issues=draft)
    assert accepted['issues'] == []
    assert accepted['paragraphs'] == data['paragraphs']
    assert accepted['terms'] == data['terms']


@pytest.mark.parametrize('first,join', [
    ('黑心虎来了。', True), ('黑心虎来了！”', True),
    ('黑心虎来了；', True), ('黑心虎来了：', True), ('黑心虎来了', False),
])
def test_cannot_borrow_identity_from_an_independent_sentence(first, join):
    data = result(['Hắc Tâm Hổ đến.', 'Hắn nhìn mọi người.'], [False, join])
    with pytest.raises(GlossaryReviewError):
        validate_result(json.dumps(data), [first, '黑心虎看着众人。'], {'黑心虎': 'Hắc Tâm Hổ'})


@pytest.mark.parametrize('name', ['hắn', 'Hắc Tâm Hồ'])
def test_join_does_not_waive_missing_or_wrong_identity(name):
    data = result([f'Dân làng vây quanh {name}', 'bị trói trên xe.'])
    with pytest.raises(GlossaryReviewError):
        validate_result(json.dumps(data), ['村民围着被绑', '的黑心虎。'], {'黑心虎': 'Hắc Tâm Hổ'})


def test_full_name_split_across_source_ids_is_still_required():
    source = ['他看着黑心', '虎。']
    good = result(['Hắn nhìn Hắc Tâm Hổ', 'đang đứng đó.'])
    assert validate_result(json.dumps(good), source, {'黑心虎': 'Hắc Tâm Hổ'})['issues'] == []
    good['paragraphs'][0]['text'] = 'Hắn nhìn gã'
    with pytest.raises(GlossaryReviewError):
        validate_result(json.dumps(good), source, {'黑心虎': 'Hắc Tâm Hổ'})


def test_amount_moves_only_within_joined_source_sentence():
    data = result(['Hắn đưa 100 đồng', 'cho ta.'])
    assert validate_result(json.dumps(data), ['他给我', '100文。'], {})['issues'] == []
    data['paragraphs'][0]['text'] = 'Hắn đưa 200 đồng'
    with pytest.raises(EditingReviewError, match='numeric literals'):
        validate_result(json.dumps(data), ['他给我', '100文。'], {})


def test_recompute_old_paragraph_findings_without_changing_prose():
    data = result(['Dân làng vây quanh Hắc Tâm Hổ', 'bị trói trên xe.'])
    data['issues'] = ['Paragraph 2: glossary mismatch: 黑心虎 -> Hắc Tâm Hổ; preserve the named entity',
                      'Joined paragraphs 1,2: glossary mismatch: obsolete']
    accepted = editing_draft(json.dumps(data), ['村民围着被绑', '的黑心虎。'], {'黑心虎': 'Hắc Tâm Hổ'}, {})
    assert accepted['issues'] == []
    assert accepted['paragraphs'] == data['paragraphs']


def test_contextual_reading_keeps_source_offsets_after_join():
    source = ['他望着', '大海。']
    data = result(['Hắn nhìn ra biển', 'mênh mông.'])
    data['glossary_readings'] = [{'paragraph_id': 2, 'source': '大海', 'source_start': 0,
        'source_quote': source[1], 'translation': 'biển',
        'reason': 'Từ này chỉ biển cả trong cảnh vật, không phải tên một nhân vật.'}]
    assert validate_result(json.dumps(data), source, {'大海': 'Đại Hải'})['issues'] == []


def test_short_contextual_reading_cannot_waive_reconstructed_full_name():
    source = ['他望着大海', '王。']
    data = result(['Hắn nhìn biển', 'ở trước mặt.'])
    data['glossary_readings'] = [{'paragraph_id': 1, 'source': '大海', 'source_start': 3,
        'source_quote': source[0], 'translation': 'biển',
        'reason': 'Từ này chỉ biển cả trong cảnh vật, không phải tên một nhân vật.'}]
    with pytest.raises(GlossaryReviewError, match='大海王'):
        validate_result(json.dumps(data), source, {'大海': 'Đại Hải', '大海王': 'Đại Hải Vương'})
