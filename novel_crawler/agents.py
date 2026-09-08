"""CLI adapters: never extract web credentials or silently switch models."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import re
import copy


MODELS = {'codex_cli': ('gpt-5.6-terra', 'medium'), 'gemini_cli': ('gemini-3.8-flash', 'high')}
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['title', 'paragraphs', 'continuity', 'issues'],
    'properties': {
        'title': {'type': 'string'},
        'paragraphs': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['id', 'text'], 'properties': {
                'id': {'type': 'integer'}, 'text': {'type': 'string'}}}},
        'continuity': {'type': 'string'},
        'issues': {'type': 'array', 'items': {'type': 'string'}},
    },
}


def output_schema(count):
    if type(count) is not int or count < 1:
        raise ValueError('Source paragraph count must be positive')
    schema = copy.deepcopy(SCHEMA)
    paragraphs = schema['properties']['paragraphs']
    paragraphs.update(minItems=count, maxItems=count)
    paragraphs['items']['properties']['id'].update(minimum=1, maximum=count)
    return schema


def executable(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f'{name} CLI not installed')
    if os.name == 'nt' and Path(path).suffix.lower() in ('.cmd', '.ps1', '.bat'):
        # Launch Node directly, avoiding cmd.exe interpretation and quoting issues.
        relative = {'codex': '@openai/codex/bin/codex.js',
                    'gemini': '@google/gemini-cli/bundle/gemini.js'}[name]
        script = Path(path).parent / 'node_modules' / relative
        node = shutil.which('node')
        if not script.is_file() or not node:
            raise RuntimeError('Unrecognized npm CLI layout; install the official CLI')
        return [node, str(script)]
    return [path]


def run_process(command, prompt, cwd, timeout, max_bytes, env):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    # Files avoid pipe deadlocks on large output. Watch their size while running.
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        stdin.write(prompt.encode('utf-8'))
        stdin.seek(0)
        p = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr,
                             cwd=cwd, env=env, creationflags=flags,
                             start_new_session=os.name != 'nt')
        import time
        deadline = time.monotonic() + timeout
        try:
            while p.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError('Agent timed out; reservation retained; no automatic retry')
                if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > max_bytes:
                    raise RuntimeError('Agent output exceeded byte limit')
                time.sleep(.1)
            if p.returncode:
                stderr.seek(0)
                detail = stderr.read(max_bytes).decode('utf-8', errors='replace')[-1800:]
                detail = re.sub(r'(?i)(Bearer\s+|sk-)[^\s"\']+', '[REDACTED]', detail)
                raise RuntimeError(f'Agent exited {p.returncode}: {detail}')
            stdout.seek(0)
            output = stdout.read(max_bytes + 1)
            if len(output) > max_bytes:
                raise RuntimeError('Agent output exceeded byte limit')
            return output.decode('utf-8')
        finally:
            if p.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'],
                                   capture_output=True, creationflags=flags, timeout=10)
                else:
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=10)


class CliAgent:
    def __init__(self, config):
        self.cfg = config
        self.provider = config['provider']
        self.model, self.effort = MODELS[self.provider]

    def preflight(self):
        command = executable('codex' if self.provider == 'codex_cli' else 'gemini')
        if self.provider == 'codex_cli':
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            status = subprocess.run(command + ['login', 'status'], capture_output=True,
                                    timeout=15, creationflags=flags)
            output = (status.stdout + status.stderr).decode('utf-8', errors='replace')
            if status.returncode or 'using ChatGPT' not in output:
                raise RuntimeError('Codex must be logged in with ChatGPT; API-key authentication is not enabled here')

    def generate(self, prompt):
        if not self.cfg['enabled']:
            raise RuntimeError('Agent disabled in config')
        with tempfile.TemporaryDirectory(prefix='novel-agent-') as temp:
            root = Path(temp)
            schema = root / 'schema.json'
            payload = json.loads(prompt.rsplit('\nDATA:\n', 1)[1])
            schema.write_text(json.dumps(output_schema(len(payload['source']))), encoding='utf-8')
            env = os.environ.copy()
            # Do not accidentally bill an API key inherited from a development shell.
            for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY',
                        'GOOGLE_GENAI_USE_VERTEXAI'):
                env.pop(key, None)
            if self.provider == 'codex_cli':
                result_file = root / 'result.json'
                command = executable('codex') + [
                    'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
                    '--sandbox', 'read-only', '--model', self.model,
                    '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
                    '-c', 'model_reasoning_effort="medium"',
                    '-c', 'features.shell_tool=false', '-c', 'features.multi_agent=false',
                    '--output-schema', str(schema), '--output-last-message', str(result_file),
                    '--json', '-']
                output = run_process(command, prompt, root, self.cfg['timeout_seconds'],
                                     self.cfg['max_output_bytes'], env)
                if not result_file.exists() or result_file.stat().st_size > self.cfg['max_output_bytes']:
                    raise ValueError('Missing or oversized agent result')
                content = result_file.read_text(encoding='utf-8')
                usage = None
                for line in output.splitlines():
                    event = json.loads(line)
                    if event.get('type') == 'turn.completed':
                        u = event.get('usage', {})
                        if 'input_tokens' in u and 'output_tokens' in u:
                            usage = u['input_tokens'] + u['output_tokens']
            else:
                settings_dir = root / '.gemini'
                settings_dir.mkdir()
                settings = {
                    'security': {'auth': {'selectedType': 'oauth-personal'}},
                    'model': {'name': self.model, 'maxSessionTurns': 1},
                    'tools': {'core': []}, 'mcp': {'allowed': []},
                    'hooksConfig': {'enabled': False},
                    'modelConfigs': {'overrides': [{
                        'match': {'model': self.model},
                        'modelConfig': {'generateContentConfig': {
                            'maxOutputTokens': self.cfg['max_output_tokens'],
                            'thinkingConfig': {'thinkingLevel': 'HIGH'},
                        }}}]},
                }
                settings_path = settings_dir / 'settings.json'
                settings_path.write_text(json.dumps(settings), encoding='utf-8')
                policy = root / 'deny-tools.toml'
                policy.write_text('[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n', encoding='utf-8')
                env['GEMINI_CLI_SYSTEM_SETTINGS_PATH'] = str(settings_path)
                command = executable('gemini') + [
                    '--model', self.model, '--prompt', 'Perform the supplied translation task; return JSON only.',
                    '--output-format', 'json', '--extensions', 'none', '--admin-policy', str(policy)]
                output = run_process(command, prompt, root, self.cfg['timeout_seconds'],
                                     self.cfg['max_output_bytes'], env)
                envelope = json.loads(output)
                if envelope.get('error'):
                    raise ValueError('Gemini returned an error')
                stats = envelope.get('stats', {}).get('models', {})
                if not stats or any(model != self.model for model in stats):
                    raise ValueError('Gemini model could not be verified or CLI silently changed models')
                usage = sum(v['tokens']['total'] for v in stats.values())
                content = envelope['response']
            return content, usage
