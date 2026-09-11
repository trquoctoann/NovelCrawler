"""One-way quota failover for a manual language-worker invocation."""
import copy
import logging
import time

from .translation import Translator, BudgetStop, classify_failure, recover


class QuotaFallbackTranslator:
    def __init__(self, store, cfg, agent_factory, stop):
        self.store, self.cfg, self.factory, self.stop = store, cfg, agent_factory, stop
        self.active = cfg['agent']['provider']
        self._select(self.active)

    def _select(self, provider):
        self.active = provider
        self.active_cfg = copy.deepcopy(self.cfg)
        self.active_cfg['agent']['provider'] = provider
        agent = self.factory(self.active_cfg['agent'])
        agent.stop = self.stop
        self.translator = Translator(self.store, self.active_cfg, agent)
        self.translator.stop = self.stop
        self.cfg['_active_agent_provider'] = provider

    def _quota_pending(self):
        db = self.store.db
        pause = db.execute('SELECT retry_at FROM provider_pauses WHERE provider=?', (self.active,)).fetchone()
        return bool((pause and pause['retry_at'] > time.time()) or db.execute(
            "SELECT 1 FROM calls WHERE provider=? AND status='failed' AND failure_kind='quota' LIMIT 1",
            (self.active,)).fetchone())

    def _can_switch(self):
        return (self.active == 'codex_cli' and self.cfg['agent'].get('fallback_provider') == 'agy'
                and not self.stop.is_set())

    def _switch(self):
        # The old provider stays paused. Only calls known to have failed for
        # quota without a returned response release their chapter for fallback.
        with self.store.transaction():
            for call in self.store.db.execute(
                    "SELECT * FROM calls WHERE provider=? AND status='failed' AND failure_kind='quota' "
                    "AND response IS NULL", (self.active,)).fetchall():
                stage = call['stage']
                if stage not in ('translate', 'edit'):
                    continue
                field, ready = ('translated', 'crawled') if stage == 'translate' else ('edited', 'translated')
                self.store.db.execute(f'UPDATE chapters SET state=?,error=NULL WHERE book_id=? AND number=? '
                                      f'AND state=? AND {field} IS NULL',
                                      (ready, call['book_id'], call['chapter'], stage + '_failed'))
                self.store.db.execute("UPDATE calls SET status='acknowledged' WHERE id=?", (call['id'],))
        self._select('agy')
        recover(self.store, self.active_cfg)
        logging.warning('Terra quota exhausted: switching translation and editing to Gemini 3.8 Flash high '
                        'via Antigravity for this manual run; Terra remains the configured primary.')

    def run(self, book, stage, batch):
        if self._can_switch() and self._quota_pending():
            self._switch()
        try:
            return self.translator.run(book, stage, batch)
        except Exception as exc:
            # Budgets, transport ambiguity and editing findings never trigger
            # a provider switch. Preflight quota errors may lack a call row.
            quota = not isinstance(exc, (BudgetStop, InterruptedError)) and classify_failure(exc, self.active_cfg)[0] == 'quota'
            if not self._can_switch() or not quota:
                raise
            if not self._quota_pending():
                self.store.db.execute('INSERT OR REPLACE INTO provider_pauses VALUES(?,?,?)',
                    (self.active, time.time() + self.active_cfg['agent'].get('quota_cooldown_seconds', 3600), str(exc)[:500]))
            self._switch()
            return self.translator.run(book, stage, batch)
