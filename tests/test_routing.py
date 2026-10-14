"""Routing experiments (scenario 3): verifiers, policy, Claude Code hooks (A / E / B), harness and probes.
Model-free: every HTTP call goes to an in-process MockTransport; claude -p is replaced by a fake runner."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from judge_adapter.routing import harness as Hn
from judge_adapter.routing import hooks as RH
from judge_adapter.routing import probe as Pr
from judge_adapter.routing import verifiers as V
from judge_adapter.routing.policy import RoutePolicy, same_model

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins' / 'davi-route'
KEY = 'sk-fake-ROUTE-KEY-not-a-secret-0123456789'


# --- policy (E: the next model as a string) -------------------------------------------------------------------------
def test_policy_escalates_below_threshold_with_next_model_string():
    pol = RoutePolicy(threshold=0.6, strong_model='opus')
    assert pol.decide(0.59).decision == 'escalate' and pol.decide(0.59).next_model == 'opus'
    assert pol.decide(0.6).decision == 'accept' and pol.decide(0.6).next_model is None


def test_policy_never_escalates_from_the_strong_model_and_fails_open_by_default():
    pol = RoutePolicy(strong_model='opus')
    assert pol.decide(0.01, current_model='claude-opus-5-5').reason == 'already_strong'
    assert pol.decide(None).decision == 'accept'
    assert RoutePolicy(on_error='escalate').decide(None).decision == 'escalate'
    assert same_model('Opus', 'opus') and not same_model('haiku', 'opus')
    with pytest.raises(ValueError):
        RoutePolicy(threshold=1.5)


# --- verifiers ------------------------------------------------------------------------------------------------------
def jev_transport(seen, p=0.27, status=200, body=None):
    def handle(req):
        seen.append(req)
        if status != 200:
            return httpx.Response(status, text=f'nope {req.headers.get("authorization")}')
        b = json.loads(req.content)
        qid = next(iter(b['questions']))
        return httpx.Response(200, json=body or {
            'model': 'jev-1.13.0', 'usage': {'input_tokens': 1000, 'output_tokens': 3},
            'answers': {qid: {'type': 'choice', 'choice': 'accept' if p >= 0.5 else 'escalate',
                              'probabilities': {'accept': p, 'escalate': 1 - p}}}})
    return httpx.MockTransport(handle)


def test_jev_wire_format_and_parse():
    seen = []
    v = V.JevVerifier(lambda: KEY, transport=jev_transport(seen, 0.27))
    r = v.verify('What is 2+2?', 'The answer is 5.', rubric='exact', kind='answer')
    (req,) = seen
    assert req.url.path == '/v1/systemone' and req.headers['authorization'] == f'Bearer {KEY}'
    body = json.loads(req.content)
    assert body['model'] == 'jev-latest' and set(body['state']) == {'task', 'candidate', 'rubric'}
    q = body['questions'][V.QUESTION_ID]
    assert q['type'] == 'choice' and set(q['criteria']) == {'accept', 'escalate'}
    assert r.outcome == 'ok' and r.p_accept == pytest.approx(0.27) and r.model == 'jev-1.13.0'
    assert r.cost_usd == pytest.approx(1000 * 0.042 / 1e6)


def test_jev_failures_are_verdicts_and_never_leak_the_key():
    r = V.JevVerifier(lambda: KEY, transport=jev_transport([], status=500)).verify('t', 'c')
    assert r.p_accept is None and r.outcome == 'http' and KEY not in (r.detail or '')
    r = V.JevVerifier(lambda: KEY, transport=jev_transport([], body={'answers': {}, 'usage': {}})).verify('t', 'c')
    assert r.outcome == 'format_error'

    def missing():
        raise V.CredentialMissing('no key')
    r = V.JevVerifier(missing, transport=jev_transport([])).verify('t', 'c')
    assert r.outcome == 'local_config' and r.cost_usd == 0.0


def claude_transport(seen, reply='23'):
    def handle(req):
        seen.append(req)
        return httpx.Response(200, json={'model': 'claude-haiku-4-5-20251001', 'stop_reason': 'end_turn',
                                         'content': [{'type': 'text', 'text': reply}],
                                         'usage': {'input_tokens': 500, 'output_tokens': 2}})
    return httpx.MockTransport(handle)


def test_claude_verbalized_probability():
    seen = []
    r = V.ClaudeVerifier(lambda: KEY, transport=claude_transport(seen, '23')).verify('t', 'c', kind='completion')
    body = json.loads(seen[0].content)
    assert seen[0].headers['x-api-key'] == KEY and body['max_tokens'] == 8 and body['temperature'] == 0.0
    assert V.KINDS['completion']['instructions'] in body['messages'][0]['content']
    assert r.p_accept == pytest.approx(0.23) and r.cost_usd == pytest.approx(500 / 1e6 + 2 * 5 / 1e6)
    bad = V.ClaudeVerifier(lambda: KEY, transport=claude_transport([], 'maybe')).verify('t', 'c')
    assert bad.outcome == 'format_error' and bad.p_accept is None
    assert V.parse_percent('100') == 1.0 and V.parse_percent('250') is None


def davi_transport(bodies):
    def handle(req):
        if '/runsync' not in req.url.path:
            return httpx.Response(404)
        b = json.loads(req.content)
        bodies.append(b)
        qid = next(iter(b['input']['questions']))
        import re
        m = re.search(r'\[\[ACCEPT=([0-9.]+)\]\]', b['input']['state']['candidate'])
        p = float(m.group(1)) if m else 0.5
        return httpx.Response(200, json={'id': 'j1', 'status': 'COMPLETED', 'delayTime': 10, 'executionTime': 400,
                                         'output': {'model': 'davi-0.2.0', 'answers': {qid: {
                                             'type': 'choice', 'choice': 'opt_accept' if p > 0.5 else 'opt_escalate',
                                             'probabilities': {'opt_accept': p, 'opt_escalate': 1 - p}}}}})
    return httpx.MockTransport(handle)


def test_davi_verifier_through_the_existing_choice_backend():
    bodies = []
    env = {'DAVI_ROUTE_RUNPOD_API_KEY': KEY, 'DAVI_ROUTE_CHOICE_ENDPOINT_ID': 'ep1', 'DAVI_ROUTE_USD_PER_SECOND': '0.001'}
    v = V.build_verifier('davi', env, transport=davi_transport(bodies))
    r = v.verify('task', 'answer [[ACCEPT=0.8]]')
    assert r.outcome == 'ok' and r.p_accept == pytest.approx(0.8) and r.model == 'davi-0.2.0'
    assert r.cost_usd == pytest.approx(0.4 * 0.001)
    q = bodies[0]['input']['questions'][V.QUESTION_ID]
    assert set(q['criteria']) == {'opt_accept', 'opt_escalate'}
    tie = v.verify('task', 'answer [[ACCEPT=0.5]]')       # exact tie: no decision, but P is still reported
    assert tie.p_accept == pytest.approx(0.5)


def test_clip_keeps_head_and_tail():
    s = 'H' * 100 + 'M' * 1000 + 'T' * 100
    c = V.clip(s, 300)
    assert c.startswith('H') and c.endswith('T') and 'omitted' in c and len(c) < 400


def test_options_precedence_override_then_plugin_option():
    env = {'DAVI_ROUTE_ROUTE_THRESHOLD': '0.7', 'CLAUDE_PLUGIN_OPTION_ROUTE_THRESHOLD': '0.2',
           'CLAUDE_PLUGIN_OPTION_ROUTE_JUDGE': 'jev'}
    s = RH.load_settings(env)
    assert s.route_threshold == 0.7 and s.route_judge == 'jev' and s.stop_judge == 'jev'
    with pytest.raises(RH.ConfigError):
        RH.load_settings({'DAVI_ROUTE_ROUTE_JUDGE': 'gpt'})


# --- hooks: A (post), E (pre), B (stop) -----------------------------------------------------------------------------
def agent_payload(event, model='haiku', result='[[ACCEPT=0.2]] wrong', session='s1', **kw):
    p = {'session_id': session, 'hook_event_name': event, 'tool_name': 'Agent', 'transcript_path': '/nonexistent',
         'tool_input': {'subagent_type': 'general-purpose', 'description': 'd', 'prompt': 'compute 17*23',
                        **({'model': model} if model else {})}}
    if event == 'PostToolUse':
        p['tool_response'] = {'content': [{'type': 'text', 'text': result}], 'totalTokens': 10}
    p.update(kw)
    return p


def settings(**kw):
    env = {'DAVI_ROUTE_ROUTE_ENABLED': 'true', 'DAVI_ROUTE_STOP_VERIFY_ENABLED': 'true'}
    env.update({f'DAVI_ROUTE_{k.upper()}': str(v) for k, v in kw.items()})
    return RH.load_settings(env)


def test_A_post_escalates_and_E_pre_applies_the_strong_model_once(tmp_path):
    st, s, ver = RH.State(str(tmp_path)), settings(), V.FakeVerifier()
    out, rec = RH.process_post(agent_payload('PostToolUse'), s, ver, st)
    ctx = out['hookSpecificOutput']['additionalContext']
    assert out['hookSpecificOutput']['hookEventName'] == 'PostToolUse' and 'model "opus"' in ctx
    assert rec['decision'] == 'escalate' and rec['next_model'] == 'opus' and rec['p_accept'] == 0.2
    assert ver.calls[0]['task'] == 'compute 17*23' and ver.calls[0]['candidate'].startswith('[[ACCEPT=0.2]]')
    out, rec = RH.process_pre(agent_payload('PreToolUse', model='haiku'), s, st)       # the re-run
    assert out['hookSpecificOutput']['updatedInput']['model'] == 'opus' and rec['source'] == 'escalation'
    assert out['hookSpecificOutput']['updatedInput']['prompt'] == 'compute 17*23'
    out, rec = RH.process_pre(agent_payload('PreToolUse', model='sonnet'), s, st)      # pending was one-shot
    assert out['hookSpecificOutput']['updatedInput']['model'] == 'haiku' and rec['source'] == 'pin_cheap'


def test_A_post_accepts_and_never_escalates_the_strong_model(tmp_path):
    st, s = RH.State(str(tmp_path)), settings()
    assert RH.process_post(agent_payload('PostToolUse', result='[[ACCEPT=0.9]] ok'), s, V.FakeVerifier(), st)[0] \
        is None
    out, rec = RH.process_post(agent_payload('PostToolUse', model='opus'), s, V.FakeVerifier(), st)
    assert out is None and rec['reason'] == 'already_strong'
    out, _ = RH.process_post(agent_payload('PostToolUse'), settings(route_post_mode='block'), V.FakeVerifier(), st)
    assert out['decision'] == 'block' and 'opus' in out['reason']
    out, rec = RH.process_post(agent_payload('PostToolUse', result='FAKE:ERROR'), s, V.FakeVerifier(), st)
    assert out is None and rec['status'] == 'verifier_failed'                         # fail-open


def test_E_pin_modes(tmp_path):
    st = RH.State(str(tmp_path))
    out, _ = RH.process_pre(agent_payload('PreToolUse', model=None), settings(route_pin_cheap='if_unset'), st)
    assert out['hookSpecificOutput']['updatedInput']['model'] == 'haiku'
    out, rec = RH.process_pre(agent_payload('PreToolUse', model='sonnet'), settings(route_pin_cheap='if_unset'), st)
    assert out is None and rec['status'] == 'unchanged'
    out, rec = RH.process_pre(agent_payload('PreToolUse', model='haiku'), settings(), st)
    assert out is None and rec['changed'] is False


def write_transcript(path, user='Fix the failing test in foo.py', assistant='Done, all tests pass.'):
    lines = [{'type': 'user', 'message': {'role': 'user', 'content': user}},
             {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'working'}]}},
             {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'x'}]}},
             {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': assistant}]}}]
    path.write_text('\n'.join(json.dumps(x) for x in lines) + '\n', encoding='utf-8')
    return str(path)


def test_transcript_reader_skips_tool_results():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = write_transcript(Path(d) / 't.jsonl')
        assert RH.transcript_messages(p) == ('Fix the failing test in foo.py', 'Done, all tests pass.')
    assert RH.transcript_messages('/nonexistent') == (None, None)


def stop_payload(tpath, last='Done [[ACCEPT=0.1]]', active=False, session='s2'):
    return {'session_id': session, 'hook_event_name': 'Stop', 'transcript_path': tpath, 'stop_hook_active': active,
            'last_assistant_message': last}


def test_B_stop_blocks_once_then_allows(tmp_path):
    st, s, ver = RH.State(str(tmp_path / 'st')), settings(), V.FakeVerifier()
    tp = write_transcript(tmp_path / 't.jsonl')
    out, rec = RH.process_stop(stop_payload(tp), s, ver, st)
    assert out['decision'] == 'block' and 'P(complete)=0.1' in out['reason'] and rec['p_accept'] == 0.1
    assert ver.calls[0]['kind'] == 'completion' and ver.calls[0]['task'] == 'Fix the failing test in foo.py'
    out, rec = RH.process_stop(stop_payload(tp, active=True), s, ver, st)
    assert out is None and rec['status'] == 'max_blocks' and len(ver.calls) == 1


def test_B_chain_guard_when_the_block_reason_becomes_a_user_message(tmp_path):
    st, s = RH.State(str(tmp_path / 'st')), settings()
    RH.process_stop(stop_payload(write_transcript(tmp_path / 'a.jsonl')), s, V.FakeVerifier(), st)
    tp2 = write_transcript(tmp_path / 'b.jsonl', user='[davi-verify] An automated completion check ...')
    out, rec = RH.process_stop(stop_payload(tp2, active=True), s, V.FakeVerifier(), st)
    assert out is None and rec['status'] == 'max_blocks'
    out, _ = RH.process_stop(stop_payload(tp2, active=False), s, V.FakeVerifier(), st)   # a new user turn
    assert out is not None


def test_B_allows_complete_tasks_and_fails_open(tmp_path):
    st, s = RH.State(str(tmp_path / 'st')), settings()
    tp = write_transcript(tmp_path / 't.jsonl')
    assert RH.process_stop(stop_payload(tp, last='done [[ACCEPT=0.9]]'), s, V.FakeVerifier(), st)[0] is None
    out, rec = RH.process_stop(stop_payload(tp, last='FAKE:ERROR'), s, V.FakeVerifier(), st)
    assert out is None and rec['decision'] == 'allow'


def test_probe_modes(tmp_path):
    st, s = RH.State(str(tmp_path)), settings(probe='true', probe_nonce='N1', probe_pre_model='haiku')
    out, rec = RH.process_post(agent_payload('PostToolUse'), s, None, st)
    assert 'DAVI-PROBE-A-N1' in out['hookSpecificOutput']['additionalContext'] and rec['tool_response_shape']
    out, rec = RH.process_pre(agent_payload('PreToolUse', model='opus'), s, st)
    assert out['hookSpecificOutput']['updatedInput']['model'] == 'haiku' and rec['model_requested'] == 'opus'
    tp = write_transcript(tmp_path / 't.jsonl')
    out, _ = RH.process_stop(stop_payload(tp), s, None, st)
    assert 'DAVI-PROBE-B-N1' in out['reason']
    out, rec = RH.process_stop(stop_payload(tp, active=True), s, None, st)
    assert out is None and rec['decision'] == 'allow'


def test_main_end_to_end_with_log(tmp_path):
    log = tmp_path / 'h.jsonl'
    env = {'DAVI_ROUTE_ROUTE_ENABLED': 'true', 'DAVI_ROUTE_LOG_FILE': str(log), 'CLAUDE_PLUGIN_DATA': str(tmp_path)}
    out, err = io.StringIO(), io.StringIO()
    RH.main('post', stdin=io.StringIO(json.dumps(agent_payload('PostToolUse'))), stdout=out, stderr=err, env=env,
            verifier=V.FakeVerifier())
    assert json.loads(out.getvalue())['hookSpecificOutput']['additionalContext']
    out2 = io.StringIO()
    RH.main('pre', stdin=io.StringIO(json.dumps(agent_payload('PreToolUse'))), stdout=out2, stderr=io.StringIO(),
            env=env)
    assert json.loads(out2.getvalue())['hookSpecificOutput']['updatedInput']['model'] == 'opus'
    summary = Hn.hooklog_summary(str(log))
    assert summary['by_kind']['post']['decision'] == {'escalate': 1}
    assert summary['by_kind']['pre']['model_set'] == {'haiku -> opus (escalation)': 1}


def test_main_is_silent_when_off_or_other_tool_or_bad_config():
    for env, payload in [({}, agent_payload('PostToolUse')),
                         ({'DAVI_ROUTE_ROUTE_ENABLED': 'true'}, {**agent_payload('PostToolUse'), 'tool_name': 'Bash'}),
                         ({'DAVI_ROUTE_ROUTE_ENABLED': 'true', 'DAVI_ROUTE_ROUTE_THRESHOLD': '7'},
                          agent_payload('PostToolUse'))]:
        out = io.StringIO()
        RH.main('post', stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=io.StringIO(), env=env,
                verifier=V.FakeVerifier())
        assert out.getvalue() == ''


def run_launcher(kind, payload, env_extra, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('DAVI_ROUTE_', 'CLAUDE_PLUGIN_OPTION_'))}
    env.update({'CLAUDE_PLUGIN_DATA': str(tmp_path), **env_extra})
    return subprocess.run([sys.executable, str(PLUGIN / 'hooks' / 'route_hook.py'), kind], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=60)


def test_launcher_fast_path_and_probe(tmp_path):
    r = run_launcher('post', agent_payload('PostToolUse'), {}, tmp_path)
    assert r.returncode == 0 and r.stdout == '' and r.stderr == ''
    r = run_launcher('post', agent_payload('PostToolUse'), {'DAVI_ROUTE_PROBE': '1', 'DAVI_ROUTE_PROBE_NONCE': 'L'},
                     tmp_path)
    assert r.returncode == 0 and 'DAVI-PROBE-A-L' in json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
    r = run_launcher('post', agent_payload('PostToolUse'), {'DAVI_ROUTE_ROUTE_ENABLED': 'true',
                                                            'DAVI_ROUTE_ROUTE_JUDGE': 'jev'}, tmp_path)
    assert r.returncode == 0 and r.stdout == ''                                  # no key: fail-open, logged
    assert json.loads(r.stderr.strip().splitlines()[-1])['outcome'] == 'local_config'


def test_plugin_manifest_matches_settings():
    plugin = json.loads((PLUGIN / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    hooks = json.loads((PLUGIN / 'hooks' / 'hooks.json').read_text(encoding='utf-8'))['hooks']
    assert set(hooks) == {'PreToolUse', 'PostToolUse', 'Stop', 'SubagentStop'}
    fields = {f for f in RH.Settings.__dataclass_fields__}
    secrets_ = {'runpod_api_key', 'choice_endpoint_id', 'usd_per_second', 'typesafe_api_key', 'anthropic_api_key'}
    cfg = plugin['userConfig']
    assert set(cfg) <= fields | secrets_
    assert all(cfg[k]['sensitive'] for k in secrets_ - {'usd_per_second'})
    defaults = RH.Settings()
    for k, spec in cfg.items():
        if k in fields and 'default' in spec:
            assert spec['default'] == getattr(defaults, k), k
    for ev, entries in hooks.items():
        for e in entries:
            (h,) = e['hooks']
            assert h['args'][0] == '${CLAUDE_PLUGIN_ROOT}/hooks/route_hook.py' and h['args'][1] in (
                'pre', 'post', 'stop', 'subagent_stop')
            assert h['timeout'] > (5 if ev == 'PreToolUse' else defaults.wall_budget_seconds)


# --- harness (C) ----------------------------------------------------------------------------------------------------
def test_graders():
    t = Hn.Task('a', 'q', answer='391', grader='number')
    assert Hn.grade(t, 'The product is 391.')[0] is True and Hn.grade(t, '17*23=390')[0] is False
    assert Hn.grade(Hn.Task('b', 'q', answer='Paris'), 'It is paris.')[0] is True
    assert Hn.grade(Hn.Task('c', 'q', answer='^ok$', grader='regex'), 'OK')[0] is True
    assert Hn.grade(Hn.Task('d', 'q'), 'anything') == (None, 'ungraded')
    assert Hn.grade(Hn.Task('e', 'q', check_cmd='grep -q 42'), 'x 42 y')[0] is True
    assert Hn.grade(Hn.Task('f', 'q', answer='x'), '   ')[0] is False


def test_claude_cmd_and_result_parsing():
    arm = Hn.parse_arm('plug=sonnet:--plugin-dir plugins/davi-route')
    cmd = Hn.claude_cmd(Hn.Task('a', 'hello'), arm, max_turns=3, budget_usd=0.5, session_id='u')
    assert cmd[:4] == ['claude', '-p', 'hello', '--model'] and cmd[4] == 'sonnet'
    assert cmd[-2:] == ['--plugin-dir', 'plugins/davi-route'] and '--max-budget-usd' in cmd and 'u' in cmd
    js = Hn.parse_result('noise\n{"type":"result","result":"OK","total_cost_usd":0.01}\n')
    assert js['result'] == 'OK'
    assert 'CLAUDE_CODE_SESSION_ID' not in Hn.child_env(arm, {'CLAUDE_CODE_SESSION_ID': 'x', 'A': '1'})


def fake_runner(answers):
    """answers: (task_id, model) -> (text, cost)."""
    def run(cmd, env, cwd, timeout):
        tid = env['T_ID'] if 'T_ID' in env else None
        prompt, model = cmd[2], cmd[cmd.index('--model') + 1]
        text, cost = answers[(prompt, model)]
        return {'exit_code': 0, 'wall_s': 1.0, 'result': text, 'is_error': False, 'total_cost_usd': cost,
                'duration_ms': 1000, 'num_turns': 1, 'session_id': 's', 'model_usage': {model: {'costUSD': cost}},
                'subagents': None, 'permission_denials': 0, 'stderr_tail': tid}
    return run


def build_cache(tmp_path, n=40):
    """Cheap answers correct on even ids; strong always correct. Fake P(accept) ranks perfectly except one case."""
    tasks, answers = [], {}
    for i in range(n):
        t = Hn.Task(f't{i}', f'q{i}', answer='yes')
        tasks.append(t)
        cheap_ok = i % 2 == 0
        p = 0.9 if cheap_ok else 0.1
        if i == 1:
            p = 0.95                                              # one confidently wrong cheap answer
        answers[(t.prompt, 'haiku')] = (('yes' if cheap_ok else 'no') + f' [[ACCEPT={p}]]', 0.01)
        answers[(t.prompt, 'opus')] = ('yes', 0.10)
    cache = str(tmp_path / 'cache')
    arms = [Hn.Arm('cheap', 'haiku'), Hn.Arm('strong', 'opus')]
    Hn.generate(tasks, arms, cache, workers=1, runner=fake_runner(answers), log=lambda *a: None)
    Hn.generate(tasks, arms, cache, workers=1, runner=None, log=lambda *a: None)          # all cached: no runner call
    Hn.judge(tasks, cache, 'cheap', ['fake'], verifier_factory=lambda n: V.FakeVerifier(cost_usd=0.0001),
             log=lambda *a: None)
    return tasks, cache


def test_analyze_baselines_auroc_and_frozen_threshold(tmp_path):
    _, cache = build_cache(tmp_path)
    rep = Hn.analyze(cache, 'cheap', 'strong', calib_frac=0.5, seed=1, tolerance=0.1, out_dir=str(tmp_path / 'r'))
    b = rep['baselines']
    assert rep['n_paired'] == 40 and b['always_cheap']['accuracy'] == 0.5 and b['always_strong']['accuracy'] == 1.0
    assert b['oracle']['accuracy'] == 1.0 and b['oracle']['cost_per_task'] == pytest.approx(0.01 + 0.5 * 0.10)
    j = rep['judges']['fake']
    assert j['auroc_cheap_correct'] == pytest.approx(1 - 20 / (20 * 20))     # one negative outranks all 20
    f = j['frozen']
    assert f['test']['accuracy'] >= f['test_random_same_rate']['accuracy']
    assert f['cost_vs_strong'] < 1.0
    md = (tmp_path / 'r' / 'report.md').read_text(encoding='utf-8')
    assert '| fake |' in md and 'always_strong' in md
    assert (tmp_path / 'r' / 'curve.csv').read_text().startswith('judge,threshold')
    assert rep['arms']['cheap']['cost_per_success'] == pytest.approx(0.4 / 20)


def test_sweep_endpoints_and_random_baseline():
    rows = [{'id': str(i), 'c': i % 2 == 0, 's': True, 'cc': 1.0, 'sc': 10.0, 'p': 0.1 * i, 'jc': 0.0}
            for i in range(10)]
    pts = Hn.sweep(rows)
    assert pts[0]['escalation_rate'] == 0.0 and pts[-1]['escalation_rate'] == 1.0
    assert pts[-1]['cost_per_task'] == pytest.approx(11.0)
    rnd = Hn._random_point(rows, 0.5)
    assert rnd['accuracy'] == pytest.approx(0.75) and rnd['cost_per_task'] == pytest.approx(6.0)
    assert Hn.auroc([0.9, 0.8, 0.1], [True, True, False]) == 1.0 and Hn.auroc([0.5], [True]) is None


def test_cli_dry_run_and_analyze(tmp_path, capsys):
    from judge_adapter.routing import cli
    tasks = tmp_path / 't.jsonl'
    tasks.write_text(json.dumps({'id': 'a', 'prompt': 'hi', 'answer': 'x'}) + '\n', encoding='utf-8')
    assert cli.main(['generate', '--tasks', str(tasks), '--cache', str(tmp_path / 'c'), '--arm', 'cheap=haiku',
                     '--dry-run']) == 0
    assert 'claude -p hi --model haiku' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(['generate', '--tasks', str(tasks), '--cache', str(tmp_path / 'c'), '--arm', 'cheap=haiku'])
    _, cache = build_cache(tmp_path)
    assert cli.main(['analyze', '--cache', cache, '--cheap', 'cheap', '--strong', 'strong']) == 0


# --- probes ---------------------------------------------------------------------------------------------------------
def test_probe_evaluation():
    n = 'N'
    results = {
        'agent': {'run': {'result': f'391 DAVI-PROBE-A-{n}', 'model_usage': {
            'claude-sonnet-5-5': {'outputTokens': 50}, 'claude-haiku-5-5': {'outputTokens': 5}},
            'subagents': {'spawned': 1}}, 'hooks': [{'kind': 'post', 'tool': 'Agent', 'model_in': 'haiku'}]},
        'stop': {'run': {'result': f'DAVI-PROBE-B-{n}', 'exit_code': 0},
                 'hooks': [{'kind': 'stop', 'decision': 'block'}, {'kind': 'stop', 'decision': 'allow'}]},
        'pre': {'run': {'model_usage': {'claude-sonnet-5-5': {'outputTokens': 9},
                                        'claude-opus-5-5': {'outputTokens': 9}}},
                'hooks': [{'kind': 'pre', 'changed': True, 'model_requested': 'opus', 'model_set': 'haiku'}]},
    }
    got = {c['id']: c['result'] for c in Pr.evaluate(results, n)}
    assert got == {'1a': 'pass', '1b': 'pass', '2': 'pass', '3a': 'pass', '3b': 'pass', '3c': 'pass', '4': 'fail'}


def test_probe_runner_with_fake_claude(tmp_path):
    """run_probes wiring: the fake runner plays the hooks by running the real launcher with the probe env."""
    def runner(cmd, env, cwd, timeout):
        prompt = cmd[2]
        if 'READY' in prompt:
            for active in (False, True):
                subprocess.run([sys.executable, str(PLUGIN / 'hooks' / 'route_hook.py'), 'stop'], env=env, text=True,
                               input=json.dumps({'session_id': 'p', 'hook_event_name': 'Stop', 'transcript_path': '',
                                                 'stop_hook_active': active, 'last_assistant_message': 'READY'}),
                               capture_output=True)
            return {'result': f'DAVI-PROBE-B-{env["DAVI_ROUTE_PROBE_NONCE"]}', 'exit_code': 0}
        model = 'opus' if 'model "opus"' in prompt else 'haiku'
        pre = subprocess.run([sys.executable, str(PLUGIN / 'hooks' / 'route_hook.py'), 'pre'], env=env, text=True,
                             input=json.dumps(agent_payload('PreToolUse', model=model)), capture_output=True)
        used = json.loads(pre.stdout)['hookSpecificOutput']['updatedInput']['model'] if pre.stdout else model
        post = subprocess.run([sys.executable, str(PLUGIN / 'hooks' / 'route_hook.py'), 'post'], env=env, text=True,
                              input=json.dumps(agent_payload('PostToolUse', model=used)), capture_output=True)
        marker = json.loads(post.stdout)['hookSpecificOutput']['additionalContext'].split()[3].rstrip('.')
        return {'result': f'391 {marker}', 'exit_code': 0, 'subagents': {'spawned': 1},
                'model_usage': {'claude-sonnet-5-5': {'outputTokens': 9}, f'claude-{used}-5-5': {'outputTokens': 3}}}
    rep = Pr.run_probes(str(PLUGIN), str(tmp_path / 'probe'), runner=runner, nonce='Z', log=lambda *a: None)
    got = {c['id']: c['result'] for c in rep['checks']}
    assert got == {k: 'pass' for k in ('1a', '1b', '2', '3a', '3b', '3c', '4')}, rep['checks']
    assert (tmp_path / 'probe' / 'probe-report.json').is_file()
