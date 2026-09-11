import json
from pathlib import Path

import pytest

from test_pipeline import setup, seed, FakeAgent
from novel_crawler.agents import CliAgent
from novel_crawler.antigravity import parse_output, AGENT_NAME
from novel_crawler.translation import reserve, BudgetStop, queue_edit_repair


def stream():
    return [{'event':'init','init':{'model':'gemini-3.8-flash-high','agent':AGENT_NAME,'tools':[]}},
            {'event':'result','result':{'status':'SUCCESS','num_turns':1,
                'structured_output':{'title':'Dịch'},'usage':{'total_tokens':123}}}]


def test_stream_checks_model_agent_usage_and_schema():
    rows=stream()
    raw,usage=parse_output('\n'.join(map(json.dumps,rows)),'gemini-3.8-flash','high')
    assert json.loads(raw)=={'title':'Dịch'} and usage==123
    for field,value in [('model','gemini-3.1-pro-high'),('agent','default')]:
        bad=stream();bad[0]['init'][field]=value
        with pytest.raises(ValueError): parse_output('\n'.join(map(json.dumps,bad)),'gemini-3.8-flash','high')
    for field,value in [('status','ERROR'),('num_turns',2),('structured_output',None),('usage',{'total_tokens':-1})]:
        bad=stream();bad[1]['result'][field]=value
        with pytest.raises((ValueError,RuntimeError)): parse_output('\n'.join(map(json.dumps,bad)),'gemini-3.8-flash','high')
    rows.insert(1,{'event':'step_update','step_update':{'step_type':'tool'}})
    with pytest.raises(ValueError,match='tool call'): parse_output('\n'.join(map(json.dumps,rows)),'gemini-3.8-flash','high')


def test_plain_response_is_preserved_for_downstream_validation():
    rows = stream()
    rows[1]['result'].pop('structured_output')
    rows[1]['result']['response'] = '{"title":"Dịch"}'
    assert json.loads(parse_output('\n'.join(map(json.dumps, rows)), 'gemini-3.8-flash', 'high')[0]) == {'title':'Dịch'}
    rows[1]['result']['response'] = '```json\n{"title":"Dịch"}\n```\n'
    assert json.loads(parse_output('\n'.join(map(json.dumps, rows)), 'gemini-3.8-flash', 'high')[0]) == {'title':'Dịch'}
    rows[1]['result']['response'] = 'Here is the result: {"title":"Dịch"}'
    raw, usage = parse_output('\n'.join(map(json.dumps, rows)), 'gemini-3.8-flash', 'high')
    assert raw == rows[1]['result']['response'] and usage == 123
    with pytest.raises(ValueError):
        json.loads(raw)


def test_process_keeps_partial_output_after_timeout(tmp_path):
    import os
    import sys
    from novel_crawler.agents import run_process
    path = tmp_path/'events.jsonl'
    with pytest.raises(TimeoutError):
        run_process([sys.executable, '-c', 'import time; print("partial", flush=True); time.sleep(30)'],
                    '', tmp_path, 1, 1000, os.environ.copy(), output_path=path)
    assert path.read_text().strip() == 'partial'


def test_recovery_reuses_only_matching_complete_paid_stream(setup):
    from novel_crawler.translation import recover
    s,cfg,book=setup; seed(s,book,1)
    cfg['agent'].update(provider='agy',workspace_dir=cfg['database'].parent/'workspaces')
    call_id=reserve(s,cfg,book['id'],1,'translate',CliAgent(cfg['agent']),'prompt',now=100000)
    call=s.db.execute('SELECT * FROM calls WHERE id=?',(call_id,)).fetchone()
    root=cfg['agent']['workspace_dir']/'test';root.mkdir(parents=True)
    metadata={'call_id':call_id,'prompt_hash':'wrong','model':call['model'],'effort':'high'}
    manifest=root/'request.json';manifest.write_text(json.dumps(metadata))
    rows=stream()
    rows[1]['result']['structured_output']={'title':'Đêm ở làng','paragraphs':[{'id':1,'text':'Hắn lặng lẽ nhìn ra ngoài sân.'}],
                                           'continuity':'Nhân vật đang ở trong nhà.','issues':[]}
    events=root/'events.jsonl';events.write_text('\n'.join(map(json.dumps,rows)))
    recover(s,cfg,now=100001)
    assert s.db.execute('SELECT response FROM calls').fetchone()[0] is None
    metadata['prompt_hash']=call['prompt_hash'];manifest.write_text(json.dumps(metadata))
    events.write_text(json.dumps(rows[0])) # Missing terminal result is never reused.
    recover(s,cfg,now=100001)
    assert s.db.execute('SELECT response FROM calls').fetchone()[0] is None
    events.write_text('\n'.join(map(json.dumps,rows)))
    recover(s,cfg,now=100001)
    assert s.db.execute('SELECT state FROM chapters').fetchone()[0]=='translated'
    row=s.db.execute('SELECT status,usage,error FROM calls').fetchone()
    assert tuple(row)==('succeeded',123,None)
    assert s.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==1


def test_agy_uses_stdin_high_effort_schema_and_durable_workspace(setup,monkeypatch):
    store,cfg,book=setup
    cfg['agent'].update(provider='agy',workspace_dir=cfg['database'].parent/'workspaces')
    monkeypatch.setenv('GEMINI_API_KEY','not-to-be-used')
    monkeypatch.setattr('novel_crawler.antigravity.executable',lambda name:['agy.exe'])
    def run(command,message,cwd,timeout,max_bytes,env,stop,output_path=None):
        assert command[command.index('--effort')+1]=='high'
        assert command[command.index('--model')+1]=='gemini-3.8-flash'
        assert command[command.index('--agent')+1]==AGENT_NAME
        assert '--dangerously-skip-permissions' not in command and '-p' not in command
        assert json.loads(message)['message']['content'].endswith('DATA:\n'+json.dumps({'source':[{'id':1,'text':'他回家。'}]}))
        assert 'GEMINI_API_KEY' not in env
        assert 'tools: []' in (cwd/'.agents/agents'/AGENT_NAME/'agent.md').read_text()
        assert '--json-schema' not in command
        assert (cwd/'schema.json').exists()
        output = '\n'.join(map(json.dumps,stream()))
        output_path.write_text(output)
        return output
    monkeypatch.setattr('novel_crawler.antigravity.run_process',run)
    raw,usage=CliAgent(cfg['agent']).generate('Rules\nDATA:\n'+json.dumps({'source':[{'id':1,'text':'他回家。'}]}))
    assert usage==123
    assert list(cfg['agent']['workspace_dir'].glob('*/events.jsonl'))


def test_switch_provider_has_separate_attempts_but_retains_global_usage(setup):
    store,cfg,book=setup;seed(store,book,1)
    for n in range(2):
        reserve(store,cfg,book['id'],1,'edit',FakeAgent(),'test',now=1000+n*100)
        store.db.execute("UPDATE calls SET status='succeeded'")
    cfg['agent']['provider']='agy'
    assert queue_edit_repair(store,cfg,book['id'],1)
    agent=CliAgent(cfg['agent'])
    reserve(store,cfg,book['id'],1,'edit',agent,'test',now=1500)
    assert store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==3
    store.db.execute("UPDATE calls SET status='succeeded'")
    cfg['limits']['calls_total']=3
    with pytest.raises(BudgetStop,match='Budget ceiling'):
        reserve(store,cfg,book['id'],2,'edit',agent,'test',now=1600)
