"""Chapter-local Gemini refusal routing; the next chapter returns to Gemini."""
import copy
import logging

from .agents import MODELS
from .fallback import QuotaFallbackTranslator
from .translation import Translator, classify_failure, source_input_hash
from .manual import require_manual, ManualInputRequired, pending_request


class LanguageTranslator:
    def __init__(self, store, cfg, factory, stop):
        self.store, self.cfg, self.factory, self.stop = store, cfg, factory, stop
        self.primary = QuotaFallbackTranslator(store, cfg, factory, stop)
        self.fallback = None

    def _blocked(self, book, chapter, stage, provider):
        return self.store.db.execute(
            "SELECT * FROM calls WHERE book_id=? AND chapter=? AND stage=? AND provider=? AND model=? "
            "AND failure_kind='content_blocked' AND status IN ('failed','acknowledged') "
            "AND source_input_hash=? AND style_revision=? ORDER BY id DESC LIMIT 1",
            (book['id'], chapter, stage, provider, MODELS[provider][0],
             source_input_hash(self.store, book['id'], chapter), self.cfg['agent'].get('style_revision', 'legacy'))).fetchone()

    def _terra(self):
        if self.fallback is None:
            cfg = copy.deepcopy(self.cfg)
            cfg['agent']['provider'] = 'codex_cli'
            cfg['agent'].pop('policy_fallback_provider', None)
            agent = self.factory(cfg['agent'])
            agent.stop = self.stop
            self.fallback = Translator(self.store, cfg, agent)
            self.fallback.stop = self.stop
        return self.fallback

    def run(self, book, stage, batch):
        if self.cfg['agent'].get('policy_fallback_provider') != 'codex_cli':
            return self.primary.run(book, stage, batch)
        count = 0
        field, ready = ('translated', 'crawled') if stage == 'translate' else ('edited', 'translated')
        for _ in range(batch):
            if self.stop.is_set() or (self.cfg['database'].parent / 'drain.request').exists():
                break
            ch = self.store.db.execute(f"SELECT * FROM chapters WHERE book_id=? AND {field} IS NULL "
                "AND state!='missing' ORDER BY number LIMIT 1", (book['id'],)).fetchone()
            if ch is None or not ch['source']:
                break
            request = pending_request(self.store, self.cfg, book, ch['number'])
            if request:
                path = require_manual(self.store, self.cfg, book, ch['number'], request['stage'], request['call_id'])
                raise ManualInputRequired('Chapter %s needs manual input: %s' % (ch['number'], path))
            # Editing needs a draft; translation of later chapters still waits
            # for prior context through the normal Translator checks.
            if stage == 'edit' and ch['translated'] is None:
                break
            blocked = self._blocked(book, ch['number'], stage, 'agy')
            if stage == 'edit' and not blocked:
                blocked = self._blocked(book, ch['number'], 'translate', 'agy')
            if not blocked:
                self.cfg['_active_agent_provider'] = 'agy'
                try:
                    done = self.primary.run(book, stage, 1)
                except Exception as exc:
                    if classify_failure(exc, self.cfg)[0] != 'content_blocked':
                        raise
                    blocked = self._blocked(book, ch['number'], stage, 'agy')
                    if not blocked:
                        raise # No durable chapter call: never route an ambiguous preflight failure.
                else:
                    count += done
                    if not done:
                        break
                    continue
            if self.stop.is_set() or (self.cfg['database'].parent / 'drain.request').exists():
                break # Save the refusal and route it on the next manual start.
            terra_blocked = self._blocked(book, ch['number'], stage, 'codex_cli')
            if terra_blocked:
                path = require_manual(self.store, self.cfg, book, ch['number'], stage, terra_blocked['id'])
                raise ManualInputRequired('Chapter %s needs manual input: %s' % (ch['number'], path))
            # Release only a known policy failure. Quota, transport failures,
            # unknown reservations and failed validations keep their retry rules.
            latest = self.store.db.execute('SELECT provider,failure_kind FROM calls WHERE book_id=? AND chapter=? '
                'AND stage=? AND source_input_hash=? AND style_revision=? ORDER BY id DESC LIMIT 1',
                (book['id'], ch['number'], stage, source_input_hash(self.store, book['id'], ch['number']),
                 self.cfg['agent'].get('style_revision', 'legacy'))).fetchone()
            if latest and latest['provider'] == 'agy' and latest['failure_kind'] == 'content_blocked':
                self.store.db.execute(f'UPDATE chapters SET state=?,error=NULL WHERE book_id=? AND number=? '
                    f'AND state=? AND {field} IS NULL', (ready, book['id'], ch['number'], stage + '_failed'))
            self.cfg['_active_agent_provider'] = 'codex_cli'
            logging.info('%s chapter %s %s: Gemini policy refusal; using Terra for this chapter',
                         book['id'], ch['number'], stage)
            try:
                done = self._terra().run(book, stage, 1)
            except Exception as exc:
                if classify_failure(exc, self.cfg)[0] != 'content_blocked':
                    raise
                failed = self._blocked(book, ch['number'], stage, 'codex_cli')
                if not failed:
                    raise
                path = require_manual(self.store, self.cfg, book, ch['number'], stage, failed['id'])
                raise ManualInputRequired('Chapter %s needs manual input: %s' % (ch['number'], path)) from exc
            count += done
            if not done:
                break
        return count
