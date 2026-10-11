"""Shared hook contracts (``plugin_common``): stub loading without httpx, the launchers' off path, the re-exported
names and patch points, and the differences that were kept on purpose. Model-free; network and key reads fail."""
from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from judge_adapter import plugin_common as PC
from judge_adapter import plugin_hook as H
from judge_adapter import prompt_signal as PS
from judge_adapter import query_hook as QH
from judge_adapter import toggles as T

ROOT = Path(__file__).resolve().parents[1]
SRC_PKG = ROOT / 'src' / 'judge_adapter'


@pytest.fixture(autouse=True)
def no_net(monkeypatch):
    def boom(*a, **k):
        raise AssertionError('network attempted')
    monkeypatch.setattr(socket.socket, 'connect', boom)
    monkeypatch.setattr(socket, 'create_connection', boom)
    monkeypatch.setattr(socket, 'getaddrinfo', boom)


def _py(*parts, env=None):
    e = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    e.update(env or {})
    code = '\n'.join(textwrap.dedent(p) for p in parts)
    r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60, env=e)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


STUB = f'''
import importlib, sys, types
stub = types.ModuleType('_davi_stub')
stub.__path__ = [{str(SRC_PKG)!r}]
sys.modules['_davi_stub'] = stub
load = lambda name: importlib.import_module('_davi_stub.' + name)
'''


# --- stub loading (launchers, davi_toggle) ----------------------------------------------------------------------------
def test_stub_loading_without_httpx_or_the_package():
    out = _py(STUB, '''
    import io, json, os, tempfile
    sys.modules['httpx'] = None                         # any "import httpx" now raises ImportError
    mods = {n: load(n) for n in ('plugin_common', 'toggles', 'prompt_signal', 'plugin_hook', 'query_hook')}
    PC = mods['plugin_common']
    env = {'CLAUDE_PLUGIN_OPTION_SEARCH_GATE_THRESHOLD': ' 0.9 ', 'JUDGE_ADAPTER_HOOK_PRESET': 'p'}
    try:
        PC.parse_num('x', 'n', 1, 0, 1)
        err = None
    except PC.ConfigError as e:
        err = str(e)
    buf = io.StringIO()
    PC.emit_json({'b': 1, 'a': 'é'}, buf, ensure_ascii=False)
    print(json.dumps({
        'httpx_loaded': sys.modules.get('httpx') is not None,
        'package_loaded': any(m == 'judge_adapter' or m.startswith('judge_adapter.') for m in sys.modules),
        'option': [PC.option(env, 'search_gate_threshold'), PC.option(env, 'preset'), PC.option(env, 'x')],
        'num': PC.parse_num('0.9', 'n', 1, 0, 1), 'err': err,
        'same_error': mods['plugin_hook'].ConfigError is PC.ConfigError is mods['query_hook'].ConfigError,
        'toggle_option': mods['toggles'].option is PC.option,
        'state': PC.state_dir({}, 'x').startswith(tempfile.gettempdir()),
        'norm': PC.normalize_text('  Ｗeb\\tSEARCH '), 'log': buf.getvalue(),
        'budget': PC.run_with_budget(lambda: 7, 5)}))
    ''')
    assert out == {'httpx_loaded': False, 'package_loaded': False, 'option': ['0.9', 'p', None], 'num': 0.9,
                   'err': 'n must be a number', 'same_error': True, 'toggle_option': True, 'state': True,
                   'norm': 'web search', 'log': '{"a": "é", "b": 1}\n', 'budget': [True, 7]}


FAST_PATH_ALLOWED = {'__future__', '_davi_stub', '_davi_stub.plugin_common', '_davi_stub.plugin_names',
                     '_davi_stub.toggles'}


def test_toggle_fast_path_imports_no_json_and_nothing_else(tmp_path):
    (tmp_path / 'toggles.json').write_text('{"filter": false, "gate": true, "updated": 1800000000.5, "v": 1}')
    out = _py('''
    import importlib, sys, types
    assert 'json' not in sys.modules                     # interpreter start-up does not load it
    before = set(sys.modules)
    ''', STUB, '''
    import os
    t = load('toggles')
    r = t.read(t.toggle_path(os.environ))
    eff = t.effective_from_env(os.environ)
    new = sorted(set(sys.modules) - before)
    json_loaded = 'json' in sys.modules
    import json
    print(json.dumps({'read': r, 'gate': eff.gate, 'filter': eff.filter, 'new': new, 'json': json_loaded}))
    ''', env={'CLAUDE_PLUGIN_DATA': str(tmp_path)})
    assert out['read'] == [{'filter': False, 'gate': True, 'updated': 1800000000.5, 'v': 1}, 'ok']
    assert out['gate'] is True and out['filter'] is False
    assert out['json'] is False
    assert set(out['new']) <= FAST_PATH_ALLOWED and '_davi_stub.plugin_common' in out['new']


def _imports(stderr):
    """Module names from ``python -X importtime`` stderr."""
    return {line.split('|')[-1].strip() for line in stderr.splitlines() if line.startswith('import time:')} - \
        {'package'}


@pytest.mark.parametrize('script,opts', [('context_filter_hook.py', {}),
                                         ('query_options_hook.py', {'SEARCH_GATE_ENABLED': 'true'}),
                                         ('user_prompt_hook.py', {'SEARCH_GATE_ENABLED': 'true'})])
def test_launcher_off_path_reads_no_stdin_imports_nothing_heavy_writes_nothing(tmp_path, script, opts):
    """Toggled off: exit 0 with no output before reading stdin (the pipe is left open), without importing the package,
    httpx, json, socket or threading, and with no file written. Reading toggles.json is the documented exception."""
    d = tmp_path / 'data'
    d.mkdir()
    (d / 'toggles.json').write_text('{"filter": false, "gate": false, "domain": false, "v": 1}')
    e = {k: v for k, v in os.environ.items() if not k.startswith(('CLAUDE_PLUGIN_OPTION_', 'JUDGE_ADAPTER_HOOK_'))
         and k != 'PYTHONPATH'}
    e.update({'CLAUDE_PLUGIN_DATA': str(d), 'CLAUDE_PLUGIN_ROOT': str(ROOT),
              'CLAUDE_PLUGIN_OPTION_RUNPOD_API_KEY': 'rpa_OFFPATH', 'CLAUDE_PLUGIN_OPTION_CHOICE_ENDPOINT_ID': 'ep',
              'CLAUDE_PLUGIN_OPTION_USD_PER_SECOND': '0.0004'})
    e.update({'CLAUDE_PLUGIN_OPTION_' + k: v for k, v in opts.items()})
    p = subprocess.Popen([sys.executable, '-X', 'importtime', str(ROOT / 'hooks' / script)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
    p.wait(timeout=30)
    out, err = p.stdout.read().decode(), p.stderr.read().decode()
    for f in (p.stdin, p.stdout, p.stderr):
        f.close()
    names = _imports(err)
    assert p.returncode == 0 and out == ''
    assert all(line.startswith('import time:') for line in err.splitlines())        # no log line either
    # importlib.import_module('_davi_stub.toggles') itself is not listed by -X importtime (only import statements
    # are); plugin_common, which only toggles imports here, shows that the toggle module was loaded.
    assert '_davi_stub.plugin_common' in names
    assert not names & {'json', 'httpx', 'socket', 'threading', 'tempfile', 'unicodedata', 'judge_adapter'}
    assert {n for n in names if n.startswith('_davi_stub.')} <= FAST_PATH_ALLOWED
    assert sorted(os.listdir(d)) == ['toggles.json']


# --- re-exports and patch points ------------------------------------------------------------------------------------
def test_shared_names_are_reexported_unchanged():
    assert H.ConfigError is PC.ConfigError is QH.ConfigError
    assert (H.KEY_ENVS, H.ENDPOINT_ENVS, H.OPTION_ENV) == (PC.KEY_ENVS, PC.ENDPOINT_ENVS, 'CLAUDE_PLUGIN_OPTION_')
    assert H.KNOWN_TOOLS_MATCHER == PC.KNOWN_TOOLS_MATCHER == '^mcp__[A-Za-z0-9_-]+__tavily_search$'
    assert T.OPTION_PREFIXES == PC.OPTION_PREFIXES == ('CLAUDE_PLUGIN_OPTION_', 'JUDGE_ADAPTER_HOOK_')
    assert H.option is PC.option and T.option is PC.option and H.run_with_budget is PC.run_with_budget
    assert QH.normalize_query is PS.normalize is PC.normalize_text


def test_module_patch_points_are_still_used_at_call_time(monkeypatch, tmp_path):
    calls = []
    real_num = H._num
    monkeypatch.setattr(H, '_num', lambda *a, **k: calls.append(a[1]) or real_num(*a, **k))
    H.load_settings({})
    assert calls == ['concurrency', 'max_docs', 'wall_budget_seconds']
    monkeypatch.setattr(H, 'run_with_budget', lambda fn, s: calls.append('budget') or (True, ('error', 'Patched')))
    err = io.StringIO()
    H.main(stdin=io.StringIO('{}'), stdout=io.StringIO(), stderr=err, env={}, exit_fn=lambda c: None,
           toggle_file=str(tmp_path / 'none.json'))
    assert calls[-1] == 'budget' and json.loads(err.getvalue())['detail'] == 'Patched'
    monkeypatch.setattr(PS, 'default_state_dir', lambda env: str(tmp_path / 'ps'))
    PS.main(stdin=io.StringIO(json.dumps({'session_id': 's', 'prompt': 'search the web'})), stderr=io.StringIO(),
            env={})
    assert len(os.listdir(tmp_path / 'ps')) == 2                              # state + lock in the patched dir
    monkeypatch.setattr(T, 'option', lambda env, key: 'false' if key == 'search_gate_enabled' else None)
    assert T.effective_from_env({}, str(tmp_path / 'none.json')).gate is False


def test_query_hook_and_davi_toggle_use_no_private_plugin_hook_names():
    for path in (SRC_PKG / 'query_hook.py', ROOT / 'hooks' / 'davi_toggle.py'):
        src = path.read_text(encoding='utf-8')
        assert not re.findall(r'\bH\._\w+', src), path
        assert not re.findall(r'\bH\.(run_with_budget|KEY_ENVS|ENDPOINT_ENVS|option|KNOWN_TOOLS_MATCHER)\b', src), path


def test_query_hook_runs_with_plugin_hook_privates_removed(monkeypatch, tmp_path):
    from test_query_hook import FakeChoice
    for name in ('_present', '_num', '_emit_log', 'run_with_budget', 'option', 'KEY_ENVS', 'ENDPOINT_ENVS'):
        monkeypatch.delattr(H, name)
    monkeypatch.setattr(H, 'load_settings', lambda env: H.Settings(usd_per_second=0.0004))
    e = {'CLAUDE_PLUGIN_OPTION_RUNPOD_API_KEY': 'rpa_X', 'CLAUDE_PLUGIN_OPTION_CHOICE_ENDPOINT_ID': 'ep',
         'CLAUDE_PLUGIN_OPTION_SEARCH_GATE_ENABLED': 'true', 'CLAUDE_PLUGIN_DATA': str(tmp_path)}
    PS.PromptState(str(tmp_path / PS.STATE_DIRNAME)).on_prompt('s1', 'Tell me about zebra lanterns, in detail.')
    fake = FakeChoice(gate={'needed': 0.02, 'not_needed': 0.97, 'cannot_tell': 0.01})
    out, err = io.StringIO(), io.StringIO()
    QH.main(stdin=io.StringIO(json.dumps({'session_id': 's1', 'hook_event_name': 'PreToolUse', 'tool_name': 'WebSearch',
                                          'tool_input': {'query': 'zebra lantern glow'}})),
            stdout=out, stderr=err, env=e, transport=httpx.MockTransport(fake.handle), exit_fn=lambda c: None)
    assert json.loads(err.getvalue())['status'] == 'denied' and fake.count() == 1
    assert json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'] == 'deny'


# --- differences kept on purpose -------------------------------------------------------------------------------------
def test_enabled_strict_in_hooks_lenient_in_toggles():
    assert T.flag_value('maybe') == 'invalid' and T.effective(lambda k: 'maybe' if k == 'enabled' else None, {}).enabled
    with pytest.raises(PC.ConfigError, match='^enabled must be true or false$'):
        H.load_settings({'CLAUDE_PLUGIN_OPTION_ENABLED': 'maybe'})
    with pytest.raises(PC.ConfigError, match='^search_gate_enabled must be true or false$'):
        QH.load_query_settings({'CLAUDE_PLUGIN_OPTION_SEARCH_GATE_ENABLED': 'maybe'})
    assert H._bool(None, True) is True and QH._flag(None, 'x') is False and QH._flag(' YES ', 'x') is True


def test_log_line_encoding_kept_per_hook(tmp_path):
    rec = {'event': 'e', 'detail': '제목'}
    a, b = io.StringIO(), io.StringIO()
    H._emit_log(rec, H.Settings(log_file=str(tmp_path / 'h.log')), a)
    PS._emit(rec, {'JUDGE_ADAPTER_HOOK_LOG_FILE': '  ' + str(tmp_path / 'p.log') + ' '}, b)
    assert a.getvalue() == '{"detail": "제목", "event": "e"}\n' == (tmp_path / 'h.log').read_text(encoding='utf-8')
    assert b.getvalue() == '{"detail": "\\uc81c\\ubaa9", "event": "e"}\n' == (tmp_path / 'p.log').read_text()
