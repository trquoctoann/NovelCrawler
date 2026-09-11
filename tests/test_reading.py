import json
import pytest
from novel_crawler.reading import reading_paragraphs, normalize_text
from novel_crawler.translation import validate_result


def test_sentence_join_and_speaker_breaks_preserve_aligned_content():
    rows=[{'id':1,'text':'Hắn chưa nói','join_previous':False},
          {'id':2,'text':'xong.\n\n“Nương tử!” “Phu quân!”','join_previous':True}]
    raw={'title':'Trò chuyện','paragraphs':rows,'continuity':'Họ gặp nhau.','issues':[]}
    result=validate_result(json.dumps(raw),['他还没说','完。“娘子！”“夫君！”'],{})
    assert [p['id'] for p in result['paragraphs']]==[1,2]
    assert reading_paragraphs(result['paragraphs'])==['Hắn chưa nói xong.','“Nương tử!”','“Phu quân!”']
    assert ''.join(''.join(p['text'].split()) for p in rows)==''.join(''.join(p.split()) for p in reading_paragraphs(rows))


def test_wrap_is_not_paragraph_break_and_quoted_words_do_not_split():
    assert normalize_text('Hắn chưa\nnói xong.')=='Hắn chưa nói xong.'
    rows=[{'id':1,'text':'Hai chữ “huynh” “đệ” viết cạnh nhau.'}]
    assert len(reading_paragraphs(rows))==1


def test_layout_cannot_drop_source_ids_or_join_first_paragraph():
    raw={'title':'Về nhà','paragraphs':[{'id':1,'text':'Hắn về nhà.','join_previous':True}],
         'continuity':'Về nhà.','issues':[]}
    with pytest.raises(ValueError,match='First paragraph'):validate_result(json.dumps(raw),['他回家。'],{})
    raw['paragraphs'][0]['join_previous']=False
    with pytest.raises(ValueError,match='coverage'):validate_result(json.dumps(raw),['他回家。','来了。'],{})
