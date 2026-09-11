import threading
import time

import pytest

from test_pipeline import setup, seed, FakeAgent
from novel_crawler.agents import MODELS
from novel_crawler.fallback import QuotaFallbackTranslator
from novel_crawler.translation import BudgetStop


def worker(setup, failures):
    s,cfg,book=setup;seed(s,book,2)
    cfg['agent'].update(provider='codex_cli',fallback_provider='agy',workspace_dir=cfg['database'].parent/'agy')
    seen=[]
    def factory(agent_cfg):
        class Agent(FakeAgent):
            provider=agent_cfg['provider']
            model=MODELS[provider][0]
            def generate(self,prompt):
                seen.append(self.provider)
                error=failures.get((self.provider,seen.count(self.provider)))
                if error:raise error
                return super().generate(prompt)
        return Agent()
    return QuotaFallbackTranslator(s,cfg,factory,threading.Event()),seen


def test_quota_during_edit_switches_both_stages_and_preserves_accounting(setup):
    t,seen=worker(setup,{('codex_cli',2):RuntimeError('usage limit reached')})
    s,cfg,book=setup
    assert t.run(book,'translate',1)==1
    assert t.run(book,'edit',1)==1
    assert t.run(book,'translate',1)==1
    assert seen==['codex_cli','codex_cli','agy','agy']
    assert cfg['agent']['provider']=='codex_cli' and cfg['_active_agent_provider']=='agy'
    rows=s.db.execute('SELECT status,failure_kind,reserved FROM calls ORDER BY id').fetchall()
    assert rows[1]['status']=='acknowledged' and rows[1]['failure_kind']=='quota' and rows[1]['reserved']>0
    assert s.db.execute('SELECT retry_at FROM provider_pauses WHERE provider="codex_cli"').fetchone()[0]>time.time()
    assert s.db.execute("SELECT COUNT(*) FROM calls WHERE status='reserved'").fetchone()[0]==0


def test_persisted_quota_uses_fallback_without_another_terra_call(setup):
    t,seen=worker(setup,{})
    s,cfg,book=setup
    s.db.execute('INSERT INTO provider_pauses VALUES(?,?,?)',('codex_cli',time.time()+3600,'quota'))
    assert t.run(book,'translate',1)==1
    assert seen==['agy']


@pytest.mark.parametrize('error',[TimeoutError('timed out'),RuntimeError('authentication required'),ValueError('bad glossary')])
def test_non_quota_failures_never_switch_provider(setup,error):
    t,seen=worker(setup,{('codex_cli',1):error})
    with pytest.raises(type(error)):t.run(setup[2],'translate',1)
    assert seen==['codex_cli'] and t.active=='codex_cli'


def test_global_budget_never_triggers_fallback(setup):
    t,seen=worker(setup,{})
    s,cfg,book=setup
    t.active_cfg['limits']['reserved_tokens_total']=1
    with pytest.raises(BudgetStop,match='Budget ceiling'):t.run(book,'translate',1)
    assert seen==[] and t.active=='codex_cli'


def test_both_providers_exhausted_stop_without_ping_pong(setup):
    t,seen=worker(setup,{('codex_cli',1):RuntimeError('quota exceeded'),('agy',1):RuntimeError('quota exceeded')})
    with pytest.raises(RuntimeError,match='quota'):t.run(setup[2],'translate',1)
    assert t.run(setup[2],'translate',1)==0
    assert seen==['codex_cli','agy']
