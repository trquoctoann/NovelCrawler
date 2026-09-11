import json

import pytest

from test_pipeline import setup, seed, FakeAgent
from novel_crawler.glossary import mismatches
from novel_crawler.translation import (Translator, GlossaryReviewError, BudgetStop,
                                       relevant_glossary, reserve, save_terms, recover)


@pytest.mark.parametrize('source,text,glossary', [
    ('九山郡城的人', 'Người từ quận thành Cửu Sơn.', {'九山郡': 'quận Cửu Sơn'}),
    ('富县的人', 'Người huyện Phú.', {'富县': 'Phú huyện'}),
    ('大王庄村民', 'Dân thôn Đại Vương.', {'大王庄': 'Đại Vương trang'}),
    ('王大虎来了', 'Vương Đại Hổ đến.', {'王大虎': 'Vương Đại Hổ', '大虎': 'Đại Hổ', '王': 'Vương'}),
    ('林风来了', 'LÂM  PHONG đến.', {'林风': 'Lâm Phong'}),
    ('刘家的人', 'Người nhà họ Lưu.', {'刘家': 'Lưu gia'}),
])
def test_identity_preserved_across_grammar_and_unicode(source, text, glossary):
    assert mismatches(source, text, glossary) == []


@pytest.mark.parametrize('source,text,glossary', [
    ('九山郡', 'Quận thành Cửu Sơn.', {'九山郡': 'quận Cửu Sơn'}),
    ('九山郡城', 'Quận thành Bát Sơn.', {'九山郡': 'quận Cửu Sơn'}),
    ('王渊', 'Vương Nguyên.', {'王渊': 'Vương Uyên'}),
    ('林风', 'Lâm Phongg.', {'林风': 'Lâm Phong'}),
    ('王大虎和大虎', 'Đại Hổ.', {'王大虎': 'Vương Đại Hổ', '大虎': 'Đại Hổ'}),
])
def test_real_identity_errors_are_still_rejected(source, text, glossary):
    assert mismatches(source, text, glossary)


def test_draft_registry_is_provisional_and_single_characters_are_guidance(setup):
    store, cfg, book = setup
    terms = {'terms': [{'source': '胡', 'translation': 'Hồ'}, {'source': '林风', 'translation': 'Lâm Phong'}]}
    save_terms(store, book['id'], 20, terms, approved=False)
    assert relevant_glossary(store, book['id'], {}, '胡说林风') == {}
    save_terms(store, book['id'], 20, terms, approved=True)
    assert relevant_glossary(store, book['id'], {}, '胡说林风', 19) == {}
    assert relevant_glossary(store, book['id'], {}, '胡说林风', 20) == {'林风': 'Lâm Phong'}


def repair_fixture(setup, always_wrong=False):
    store, cfg, book = setup
    seed(store, book, 1)
    store.db.execute('UPDATE chapters SET source=?', (json.dumps(['王渊回来了。', '李诗涵来了。']),))
    book['glossary'].write_text(json.dumps({'王渊': 'Vương Uyên', '李诗涵': 'Lý Thi Hàm'}), encoding='utf-8')
    class Agent(FakeAgent):
        def generate(self, prompt):
            payload = json.loads(prompt.split('\nDATA:\n')[1]); self.prompts.append(payload)
            repair = payload.get('repair_findings')
            wrong = payload['draft'] is not None and (always_wrong or not repair)
            if repair:
                assert 'Paragraph 1' in repair and 'Paragraph 2' in repair
            return json.dumps({'title': 'Trở về', 'paragraphs': [
                {'id': 1, 'text': 'Vương Nguyên đã về.' if wrong else 'Vương Uyên đã về.'},
                {'id': 2, 'text': 'Lý Thi Hàn đã đến.' if wrong else 'Lý Thi Hàm đã đến.'}],
                'continuity': 'Hai người trở về.', 'issues': [], 'terms': []}), 100
    agent = Agent(); translator = Translator(store, cfg, agent)
    translator.run(book, 'translate', 1)
    return store, cfg, book, translator


def test_automatic_repair_gets_all_findings_and_retains_paid_results(setup):
    store, cfg, book, translator = repair_fixture(setup)
    assert translator.run(book, 'edit', 1) == 1
    rows = store.db.execute('SELECT * FROM calls ORDER BY id').fetchall()
    assert [r['status'] for r in rows] == ['succeeded', 'acknowledged', 'succeeded']
    assert all(r['response'] and r['glossary_context'] and r['reserved'] > 0 for r in rows)
    assert store.db.execute('SELECT state FROM chapters').fetchone()[0] == 'edited'
    recover(store, cfg)
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 3


def test_bad_repair_is_bounded_even_with_unlimited_budget_and_restart(setup):
    store, cfg, book, translator = repair_fixture(setup, always_wrong=True)
    for key in ('calls_per_day', 'calls_total', 'reserved_tokens_per_day', 'reserved_tokens_total'):
        cfg['limits'][key] = 0
    with pytest.raises(GlossaryReviewError):
        translator.run(book, 'edit', 1)
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 3
    recover(store, cfg)
    assert translator.run(book, 'edit', 1) == 0
    assert store.db.execute('SELECT state FROM chapters').fetchone()[0] == 'edit_failed'


def test_unlimited_budget_still_accounts_usage_and_enforces_attempts(setup):
    store, cfg, book = setup
    for key in ('calls_per_day', 'calls_total', 'reserved_tokens_per_day', 'reserved_tokens_total'):
        cfg['limits'][key] = 0
    for n in range(55):
        reserve(store, cfg, book['id'], n, 'edit', FakeAgent(), 'x', now=100000 + n * 100)
        store.db.execute("UPDATE calls SET status='succeeded'")
    assert store.db.execute('SELECT COUNT(*),SUM(reserved) FROM calls').fetchone()[1] > 0
    cfg['limits']['max_attempts_per_chapter_stage'] = 1
    with pytest.raises(BudgetStop, match='attempts'):
        reserve(store, cfg, book['id'], 0, 'edit', FakeAgent(), 'x', now=200000)


def test_recovery_uses_call_snapshot_even_if_later_registry_differs(setup):
    store, cfg, book = setup
    seed(store, book, 1)
    agent = FakeAgent()
    assert Translator(store, cfg, agent).run(book, 'translate', 1) == 1
    store.db.execute("UPDATE calls SET status='reserved'")
    store.db.execute("UPDATE chapters SET translated=NULL,state='translate_inflight'")
    save_terms(store, book['id'], 1, {'terms': [{'source': '院子', 'translation': 'viện tử'}]})
    recover(store, cfg)
    result = json.loads(store.db.execute('SELECT translated FROM chapters').fetchone()[0])
    assert result['issues'] == []
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 1


def test_zero_budget_config_is_explicit_and_other_guards_remain_positive(tmp_path):
    from pathlib import Path
    import re
    from novel_crawler.config import load
    content = (Path(__file__).parents[1] / 'config.example.toml').read_text(encoding='utf-8')
    for k in ('calls_per_day', 'calls_total', 'reserved_tokens_per_day', 'reserved_tokens_total'):
        content = re.sub(r'(?m)^' + k + r' = \d+', k + ' = 0', content)
    path = tmp_path / 'config.toml'; path.write_text(content, encoding='utf-8')
    assert load(str(path))['limits']['calls_total'] == 0
    path.write_text(content.replace('max_attempts_per_chapter_stage = 2', 'max_attempts_per_chapter_stage = 0'), encoding='utf-8')
    with pytest.raises(ValueError):
        load(str(path))


def test_editor_does_not_follow_stale_machine_glossary_warnings():
    from novel_crawler.translation import editing_draft
    raw = json.dumps({'title': 'Lời nói', 'paragraphs': [{'id': 1, 'text': 'Vương Nguyên đừng nói bừa.'}],
        'continuity': 'Tranh luận.', 'issues': ['Paragraph 1: glossary mismatch: 胡 -> Hồ; use the locked rendering',
                                            'Source speaker is ambiguous.']})
    result = editing_draft(raw, ['王渊别胡说。'], {'王渊': 'Vương Uyên'}, {})
    assert 'Source speaker is ambiguous.' in result['issues']
    assert not any('胡 -> Hồ' in issue for issue in result['issues'])
    assert any('王渊 -> Vương Uyên' in issue for issue in result['issues'])


@pytest.mark.parametrize('source,text', [
    ('刘家大少爷', 'Lưu đại thiếu gia'),
    ('周家三老爷', 'Châu tam lão gia'),
    ('王家二小姐', 'Vương nhị tiểu thư'),
    ('刘家夫人', 'Lưu phu nhân'),
])
def test_family_title_rendering_requires_exact_source_context(source, text):
    from novel_crawler.glossary import equivalent, requirements
    families = {'刘家': 'Lưu gia', '周家': 'Châu gia', '王家': 'Vương gia'}
    assert not mismatches(source, text, families)
    assert text.casefold() in requirements(source, families)[0]['accepted_renderings']
    # A contextual title never replaces the canonical family in the registry.
    family = source[:2]
    assert not equivalent(family, families[family], text)


@pytest.mark.parametrize('source,text', [
    ('刘家大少爷', 'Châu đại thiếu gia'),
    ('刘家大少爷', 'Lưu nhị thiếu gia'),
    ('刘家大少爷', 'Lưu đại tiểu thư'),
    ('刘家', 'Lưu đại thiếu gia'),
    ('刘家的人', 'Lưu'),
    ('刘家大少爷', 'Lưu'),
])
def test_family_title_does_not_allow_wrong_name_rank_or_missing_family(source, text):
    assert mismatches(source, text, {'刘家': 'Lưu gia'})


def test_alias_cannot_overwrite_another_source_name_in_same_paragraph():
    from novel_crawler.translation import validate_result
    source = ['大王村那个小童生太厉害了，一连两次让大王庄告状人都没出面。']
    text = 'Tiểu đồng sinh ở thôn Đại Vương kia quá lợi hại. Liên tiếp hai lần khiến người Đại Vương trang đi tố cáo đều không cần tự mình lộ diện.'
    raw = {'title': 'Lời bàn', 'paragraphs': [{'id': 1, 'text': text}], 'continuity': 'Mọi người bàn luận.', 'issues': []}
    glossary = {'大王村': 'thôn Đại Vương', '大王庄': 'Đại Vương trang'}
    aliases = {'大王庄': ['thôn Đại Vương']}
    result = validate_result(json.dumps(raw), source, glossary, rendering_aliases=aliases)
    assert result['paragraphs'][0]['text'] == text
    raw['paragraphs'][0]['text'] = text.replace('thôn Đại Vương', 'thôn Tiểu Vương')
    with pytest.raises(GlossaryReviewError, match='大王村'):
        validate_result(json.dumps(raw), source, glossary, rendering_aliases=aliases)


def test_alias_normalization_preserves_longer_mentions_and_does_not_cascade():
    from novel_crawler.glossary import normalize_aliases
    glossary = {'甲城': 'thành Giáp', '乙城': 'thành Ất'}
    aliases = {'甲城': ['Giáp thành'], '乙城': ['thành Giáp']}
    # Shared names are ambiguous and cannot be reassigned to the second city.
    assert normalize_aliases('甲城和乙城', 'Giáp thành và thành Ất', glossary, aliases) == 'thành Giáp và thành Ất'
    glossary = {'王庄': 'Vương trang', '小王庄': 'Tiểu Vương trang'}
    assert normalize_aliases('小王庄', 'thôn Vương', glossary, {'王庄': ['thôn Vương']}) == 'thôn Vương'
    # Also protect a variant embedded in another name that is actually mentioned.
    glossary = {'甲村': 'Giáp thôn', '乙村': 'thôn Giáp mới'}
    assert normalize_aliases('甲村和乙村', 'thôn Giáp mới và Giáp thôn', glossary, {'甲村': ['thôn Giáp']}) == 'thôn Giáp mới và Giáp thôn'


def test_unambiguous_alias_still_normalizes_with_unicode_and_word_boundaries():
    import unicodedata
    from novel_crawler.glossary import normalize_aliases
    glossary = {'大王庄': 'Đại Vương trang'}
    aliases = {'大王庄': ['thôn Đại Vương']}
    text = unicodedata.normalize('NFC', 'THÔN  ĐẠI VƯƠNG ở đây; thôn Đại Vươngg ở kia.')
    assert normalize_aliases('大王庄', text, glossary, aliases) == 'Đại Vương trang ở đây; thôn Đại Vươngg ở kia.'


@pytest.mark.parametrize('source', [
    '左相的幕僚都没去，给你小小山贼当军师，瞎了心吧！',
    '三虎山大小山贼接近一百。',
])
def test_short_names_do_not_cut_chinese_words(source):
    from novel_crawler.glossary import requirements
    glossary = {'小山': 'Tiểu Sơn', '马前': 'Mã Tiền', '四海': 'Tứ Hải'}
    assert requirements(source, glossary) == []
    assert mismatches(source, 'Câu văn thông thường.', glossary) == []


@pytest.mark.parametrize('source,glossary', [
    ('小山来了。', {'小山': 'Tiểu Sơn'}),
    ('青山叔、小山叔以后来我这边。', {'青山叔': 'Thanh Sơn thúc', '小山': 'Tiểu Sơn'}),
    ('大海、二海、三海、青山、小山、五服内叔伯。', {'大海': 'Đại Hải', '小山': 'Tiểu Sơn'}),
    ('王小山回来了。', {'王小山': 'Vương Tiểu Sơn', '小山': 'Tiểu Sơn'}),
    ('王渊和林风回来了。', {'王渊': 'Vương Uyên', '林风': 'Lâm Phong'}),
    ('大王庄村民来了。', {'大王庄': 'Đại Vương trang'}),
])
def test_real_names_remain_required_even_if_tokenizer_does_not_know_them(source, glossary):
    from novel_crawler.glossary import requirements
    assert requirements(source, glossary)
    assert mismatches(source, 'Ai đó trở về.', glossary)


def test_prompt_validation_and_term_admission_use_same_source_mentions(setup):
    from novel_crawler.translation import relevant_glossary, validate_result
    store, cfg, book = setup
    save_terms(store, book['id'], 1, {'terms': [{'source': '小山', 'translation': 'Tiểu Sơn'}]})
    source = '给你小小山贼当军师，瞎了心吧！'
    assert relevant_glossary(store, book['id'], {}, source) == {}
    raw = {'title': 'Sơn tặc', 'paragraphs': [{'id': 1, 'text': 'Làm quân sư cho đám sơn tặc bé tí các ngươi, đúng là nghĩ hay!'}],
           'continuity': 'Bàn chuyện sơn tặc.', 'issues': [],
           'terms': [{'source': '小山', 'translation': 'Tiểu Sơn'}]}
    assert validate_result(json.dumps(raw), [source], {'小山': 'Tiểu Sơn'})['terms'] == []
    # Even a model that hallucinates that name cannot add it based on a substring.
    raw['paragraphs'][0]['text'] += ' Tiểu Sơn.'
    assert validate_result(json.dumps(raw), [source], {})['terms'] == []
    assert relevant_glossary(store, book['id'], {}, '小山叔回来了。') == {'小山': 'Tiểu Sơn'}


@pytest.mark.parametrize('source,zh', [
    ('他甘当马前卒。', '马前'), ('朋友来自五湖四海。', '四海'),
    ('大虎将钱箱向前一推。', '大虎'), ('王渊名字在册。', '王渊'),
    ('赵武松了口气。', '赵武'), ('两辆马车驶入富县城。', '富县'),
    ('他们想统一大业。', '大业'),
])
def test_tokenizer_uncertainty_cannot_silently_remove_name_check(source, zh):
    from novel_crawler.glossary import requirements
    assert requirements(source, {zh: 'Tên đã duyệt'})
    assert mismatches(source, 'Không có tên đã duyệt trong câu.', {zh: 'Tên cố định'})


def ordinary_fixture():
    source = '这样便可高枕无忧。'
    raw = {'title': 'Yên ổn', 'paragraphs': [{'id': 1, 'text': 'Như vậy có thể kê cao gối ngủ, không phải lo lắng.'}],
           'continuity': 'Mọi người an tâm.', 'issues': [], 'glossary_readings': [{
               'paragraph_id': 1, 'source': '无忧', 'source_start': source.index('无忧'),
               'source_quote': source, 'translation': 'không phải lo lắng',
               'reason': 'Nằm trong thành ngữ nói về sự an tâm, không chỉ một người mang tên Vô Ưu.'}]}
    return source, raw


def test_contextual_reading_is_scoped_and_cannot_change_registry():
    from novel_crawler.translation import validate_result
    source, raw = ordinary_fixture()
    raw['terms'] = [{'source': '无忧', 'translation': 'không phải lo lắng', 'kind': 'entity'}]
    result = validate_result(json.dumps(raw), [source], {'无忧': 'Vô Ưu'})
    assert result['glossary_readings'] == raw['glossary_readings']
    assert result['terms'] == []
    raw['glossary_readings'] = []
    with pytest.raises(GlossaryReviewError):
        validate_result(json.dumps(raw), [source], {'无忧': 'Vô Ưu'})


@pytest.mark.parametrize('field,value', [
    ('source_start', 0), ('source_quote', 'Nguồn khác'), ('paragraph_id', 2),
    ('reason', ''),
])
def test_ungrounded_contextual_reading_is_rejected(field, value):
    from novel_crawler.translation import validate_result
    source, raw = ordinary_fixture(); raw['glossary_readings'][0][field] = value
    with pytest.raises(ValueError):
        validate_result(json.dumps(raw), [source], {'无忧': 'Vô Ưu'})


def test_ordinary_reading_cannot_hide_wrong_numbers_or_unresolved_issues():
    from novel_crawler.translation import validate_result
    source, raw = ordinary_fixture()
    raw['paragraphs'][0]['text'] += ' Có 20 người.'
    with pytest.raises(ValueError, match='numeric'):
        validate_result(json.dumps(raw), [source], {'无忧': 'Vô Ưu'})
    source, raw = ordinary_fixture(); raw['issues'] = ['Missing material.']
    with pytest.raises(ValueError, match='Agent reported issues'):
        validate_result(json.dumps(raw), [source], {'无忧': 'Vô Ưu'})


def test_ordinary_reading_does_not_waive_another_occurrence_or_full_name():
    from novel_crawler.translation import validate_result
    source, raw = ordinary_fixture()
    source += '无忧走来了。'
    raw['glossary_readings'][0]['source_quote'] = source
    raw['paragraphs'][0]['text'] += ' Vô Ưu bước tới.'
    assert validate_result(json.dumps(raw), [source], {'无忧': 'Vô Ưu'})
    raw['paragraphs'][0]['text'] = raw['paragraphs'][0]['text'].replace('Vô Ưu', 'Vô Ưu Sai')
    # Replace the exact name completely to exercise the other occurrence.
    raw['paragraphs'][0]['text'] = raw['paragraphs'][0]['text'].replace('Vô Ưu Sai', 'Ai đó')
    with pytest.raises(GlossaryReviewError):
        validate_result(json.dumps(raw), [source], {'无忧': 'Vô Ưu'})
    source, raw = ordinary_fixture()
    source = '王小山来了。'; raw['glossary_readings'][0].update(source='王小山', source_start=0, source_quote=source)
    with pytest.raises(GlossaryReviewError, match='王小山'):
        validate_result(json.dumps(raw), [source], {'王小山': 'Vương Tiểu Sơn'})



def test_contextual_reading_survives_recovery_and_readonly_audit(setup):
    from novel_crawler.glossary_audit import audit
    source, raw = ordinary_fixture()
    store, cfg, book = setup
    seed(store, book, 1)
    book['glossary'].write_text(json.dumps({'无忧': 'Vô Ưu'}), encoding='utf-8')
    store.db.execute('UPDATE chapters SET source=?', (json.dumps([source]),))
    class Agent(FakeAgent):
        def generate(self, prompt):
            return json.dumps(raw), 100
    translator = Translator(store, cfg, Agent())
    translator.run(book, 'translate', 1)
    translator.run(book, 'edit', 1)
    store.db.execute("UPDATE chapters SET edited=NULL,state='edit_inflight'")
    store.db.execute("UPDATE calls SET status='reserved' WHERE stage='edit'")
    recover(store, cfg)
    edited = json.loads(store.db.execute('SELECT edited FROM chapters').fetchone()[0])
    assert edited['glossary_readings'] == raw['glossary_readings']
    before = list(store.db.iterdump())
    result = audit(cfg)
    assert result['edited_checked'] == 1 and result['edited_validation_errors'] == []
    assert list(store.db.iterdump()) == before
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 2


def test_ordinary_reading_commentary_need_not_match_idiomatic_word_order():
    from novel_crawler.translation import validate_result
    source,raw=ordinary_fixture()
    raw['glossary_readings'][0]['translation']='không lo nghĩ, được an tâm'
    assert validate_result(json.dumps(raw),[source],{'无忧':'Vô Ưu'})['paragraphs']==raw['paragraphs']


@pytest.mark.parametrize('annotation', [None, 'bad metadata', [None], [{'source': []}],
    [{'paragraph_id': True, 'source': '无忧', 'source_start': 0,
      'source_quote': '无忧来了。', 'translation': 'ai đó', 'reason': 'Không có bằng chứng thực tế.'}]])
def test_invalid_optional_readings_do_not_block_correct_prose_or_waive_names(annotation):
    from novel_crawler.translation import validate_result
    source, raw = ordinary_fixture()
    raw['glossary_readings'] = annotation
    raw['paragraphs'][0]['text'] = 'Vô Ưu đã tới.'
    result = validate_result(json.dumps(raw), ['无忧来了。'], {'无忧': 'Vô Ưu'})
    assert result['glossary_readings'] == []
    assert result['paragraphs'] == raw['paragraphs']
    raw['paragraphs'][0]['text'] = 'Ai đó đã tới.'
    with pytest.raises(GlossaryReviewError):
        validate_result(json.dumps(raw), ['无忧来了。'], {'无忧': 'Vô Ưu'})


def test_invalid_reading_still_reaches_independent_contextual_review():
    from novel_crawler.glossary_review import targets
    source, raw = ordinary_fixture()
    raw['glossary_readings'][0]['source_quote'] = 'Only a partial quote'
    found = targets([source], raw, {'无忧': 'Vô Ưu'}, [])
    assert len(found) == 1 and found[0]['source'] == '无忧'
