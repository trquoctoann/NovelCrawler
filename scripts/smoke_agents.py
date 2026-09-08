"""Opt-in live integration probe, max two attempts/provider, same global quota DB."""
import json
from pathlib import Path
import time

from novel_crawler.agents import CliAgent, SCHEMA
from novel_crawler.config import load
from novel_crawler.store import Store, pipeline_lock
from novel_crawler.translation import reserve, validate_result
from novel_crawler.runtime import ensure_config


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', choices=['codex_cli', 'gemini_cli'], required=True)
    args = parser.parse_args()
    cfg = load(str(ensure_config('config.toml')))
    cfg['agent'] = dict(cfg['agent'], provider=args.provider, timeout_seconds=120, max_output_tokens=2500)
    source = ['夜里下起了小雨。', '他关上窗户，继续读书。']
    rules = (Path(__file__).parents[1] / 'novel_crawler/prompts/translation/SKILL.md').read_text(encoding='utf-8')
    prompt = rules + '\nOUTPUT SCHEMA:\n' + json.dumps(SCHEMA) + '\nDATA:\n' + json.dumps({
        'title': '夜雨', 'glossary': {}, 'previous_context': None,
        'source': [{'id': i, 'text': p} for i, p in enumerate(source, 1)]}, ensure_ascii=False)
    with pipeline_lock(cfg['database']):
        store = Store(cfg['database'])
        try:
            book_id = 'integration-probe-' + args.provider
            prior = store.db.execute('SELECT status FROM calls WHERE book_id=? ORDER BY id DESC LIMIT 1', (book_id,)).fetchone()
            if prior and prior['status'] == 'succeeded':
                print('Already passed; no further inference.')
                return
            agent = CliAgent(cfg['agent'])
            agent.preflight()
            last = store.db.execute('SELECT MAX(created) FROM calls').fetchone()[0]
            if last:
                time.sleep(max(0, cfg['limits']['min_call_interval_seconds'] - (time.time() - last)))
            call_id = reserve(store, cfg, book_id, 1, 'smoke', agent, prompt)
            try:
                raw, usage = agent.generate(prompt)
                store.db.execute('UPDATE calls SET response=?,usage=?,reserved=MAX(reserved,?) WHERE id=?', (raw, usage, usage or 0, call_id))
                result = validate_result(raw, source, {})
                store.db.execute("UPDATE calls SET status='succeeded' WHERE id=?", (call_id,))
                print(json.dumps({'provider': args.provider, 'model': agent.model, 'effort': agent.effort,
                                  'usage': usage, 'result': result}, ensure_ascii=True))
            except Exception:
                store.db.execute("UPDATE calls SET status='failed' WHERE id=?", (call_id,))
                raise
        finally:
            store.close()


if __name__ == '__main__':
    main()
