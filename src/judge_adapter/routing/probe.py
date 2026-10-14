# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Mechanism probes for the davi-route hooks in a real Claude Code (headless ``claude -p``). No verifier is called.

Three short runs with ``--plugin-dir plugins/davi-route`` and ``DAVI_ROUTE_PROBE=1``:

run ``agent``   main model asks one general-purpose subagent with model "haiku" for 17*23.
    1a  PostToolUse fires on the Agent tool                       (hook log: post record)
    1b  its additionalContext reaches the main agent              (marker DAVI-PROBE-A-<nonce> in the answer)
    2   the per-call model "haiku" is accepted for the subagent   (hook log model_in, modelUsage has a haiku model,
                                                                   subagent_stats.spawned >= 1)
run ``stop``    "Reply with exactly READY."
    3a  Stop fires and can block with a reason                    (hook log: block)
    3b  the block reason is acted on                              (marker DAVI-PROBE-B-<nonce> in the answer)
    3c  the loop guard ends it                                    (a later stop record allows; run exits normally)
run ``pre``     main model asks a subagent with model "opus"; the PreToolUse probe rewrites it to "haiku".
    4   updatedInput on the Agent tool changes the subagent model (no opus model in modelUsage; haiku present)

Model usage is read from the ``modelUsage`` of the result. Caveat: Claude Code can use a small model for side
tasks, so "haiku present" alone is weak evidence; check 4 ("opus absent although requested") is the strong one.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time

from . import harness as Hn

AGENT_PROMPT = ('Use the Agent tool exactly once with subagent_type "general-purpose", model "{model}" and the prompt '
                '"What is 17*23? Reply with the number only." When it returns, report the subagent\'s answer. If any '
                'hook output or system message shows a marker starting with DAVI-PROBE-A-, end your reply with that '
                'marker verbatim.')
STOP_PROMPT = 'Reply with exactly the word READY.'


def _has(mu, name):
    return any(name in m.lower() and (u.get('outputTokens') or 0) > 0 for m, u in (mu or {}).items())


def run_probes(plugin_dir, out_dir, main_model='sonnet', claude_bin='claude', runner=Hn.run_claude, nonce=None,
               timeout=600, log=print, which=('agent', 'stop', 'pre')):
    nonce = nonce or secrets.token_hex(4)
    os.makedirs(out_dir, exist_ok=True)
    logf = os.path.join(out_dir, 'probe-hooks.jsonl')
    work = tempfile.mkdtemp(prefix='davi-route-probe-')
    base = {'DAVI_ROUTE_PROBE': '1', 'DAVI_ROUTE_PROBE_NONCE': nonce, 'DAVI_ROUTE_LOG_FILE': logf,
            'DAVI_ROUTE_STOP_SUBAGENTS': 'false', 'DAVI_ROUTE_STATE_DIR': os.path.join(work, 'state')}
    plan = {
        'agent': (main_model, AGENT_PROMPT.format(model='haiku'), {'DAVI_ROUTE_PROBE_STOP': 'false'}),
        'stop': ('haiku', STOP_PROMPT, {}),
        'pre': (main_model, AGENT_PROMPT.format(model='opus'), {'DAVI_ROUTE_PROBE_PRE_MODEL': 'haiku',
                                                                  'DAVI_ROUTE_PROBE_STOP': 'false'}),
    }
    results = {}
    for name in which:
        model, prompt, extra = plan[name]
        arm = Hn.Arm(name, model, ['--plugin-dir', os.path.abspath(plugin_dir)], {**base, **extra,
                                                                                    'DAVI_ROUTE_RUN': name})
        task = Hn.Task(id=f'probe-{name}', prompt=prompt)
        before = len(Hn.read_jsonl(logf))
        log(f'probe run {name}: claude -p --model {model} --plugin-dir {plugin_dir}')
        r = runner(Hn.claude_cmd(task, arm, claude_bin, max_turns=8), Hn.child_env(arm), work, timeout)
        hooks = [h for h in Hn.read_jsonl(logf)[before:] if h.get('event') == 'davi-route']
        results[name] = {'run': r, 'hooks': hooks}
        time.sleep(0.2)
    checks = evaluate(results, nonce)
    rep = {'nonce': nonce, 'main_model': main_model, 'plugin_dir': os.path.abspath(plugin_dir), 'checks': checks,
           'runs': {k: {'result': v['run'].get('result'), 'exit_code': v['run'].get('exit_code'),
                        'model_usage': v['run'].get('model_usage'), 'subagents': v['run'].get('subagents'),
                        'total_cost_usd': v['run'].get('total_cost_usd'), 'hooks': v['hooks']}
                    for k, v in results.items()}}
    with open(os.path.join(out_dir, 'probe-report.json'), 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
    return rep


def _c(cid, what, ok, evidence):
    return {'id': cid, 'check': what, 'result': 'pass' if ok is True else ('fail' if ok is False else 'unknown'),
            'evidence': evidence}


def evaluate(results, nonce):
    out = []
    a = results.get('agent')
    if a:
        posts = [h for h in a['hooks'] if h.get('kind') == 'post']
        res = a['run'].get('result') or ''
        mu = a['run'].get('model_usage') or {}
        spawned = (a['run'].get('subagents') or {}).get('spawned')
        out.append(_c('1a', 'PostToolUse fires on the Agent tool', bool(posts),
                      {'post_records': len(posts), 'tools': sorted({str(h.get('tool')) for h in posts}),
                       'tool_response_shape': posts[0].get('tool_response_shape') if posts else None}))
        out.append(_c('1b', 'PostToolUse additionalContext reaches the main agent',
                      (f'DAVI-PROBE-A-{nonce}' in res) if posts else None, {'answer_tail': res[-200:]}))
        model_in = [h.get('model_in') for h in posts]
        resolved = [h.get('resolved_model') for h in posts]
        ok2 = None if not posts else ('haiku' in [str(m).lower() for m in model_in] and _has(mu, 'haiku')
                                      and (spawned or 0) >= 1
                                      and all('haiku' in str(r).lower() for r in resolved if r))
        out.append(_c('2', 'Per-call subagent model "haiku" is accepted', ok2,
                      {'model_in': model_in, 'resolved_model': resolved, 'models_used': sorted(mu),
                       'subagents_spawned': spawned,
                       'answer_has_391': '391' in res}))
    s = results.get('stop')
    if s:
        stops = [h for h in s['hooks'] if h.get('kind') == 'stop']
        res = s['run'].get('result') or ''
        blocks = [h for h in stops if h.get('decision') == 'block']
        allows = [h for h in stops if h.get('decision') == 'allow']
        out.append(_c('3a', 'Stop hook fires and blocks with a reason', bool(blocks),
                      {'stop_records': len(stops), 'payload_keys': stops[0].get('payload_keys') if stops else None,
                       'has_last_assistant_message': [h.get('has_last_assistant_message') for h in stops],
                       'stop_hook_active': [h.get('stop_hook_active') for h in stops]}))
        out.append(_c('3b', 'The block reason is acted on', (f'DAVI-PROBE-B-{nonce}' in res) if blocks else None,
                      {'answer_tail': res[-200:]}))
        out.append(_c('3c', 'Loop guard: a later stop is allowed and the run ends',
                      (bool(allows) and s['run'].get('exit_code') == 0) if blocks else None,
                      {'allows': len(allows), 'exit_code': s['run'].get('exit_code')}))
    p = results.get('pre')
    if p:
        pres = [h for h in p['hooks'] if h.get('kind') == 'pre']
        mu = p['run'].get('model_usage') or {}
        changed = [h for h in pres if h.get('changed')]
        resolved = [h.get('resolved_model') for h in p['hooks'] if h.get('kind') == 'post' and h.get('resolved_model')]
        ok4 = None if not changed else (not _has(mu, 'opus') and _has(mu, 'haiku')
                                        and not any('opus' in str(r).lower() for r in resolved))
        out.append(_c('4', 'PreToolUse updatedInput changes the subagent model (opus requested -> haiku)', ok4,
                      {'pre_records': len(pres), 'rewrites': [(h.get('model_requested'), h.get('model_set'))
                                                              for h in changed], 'resolved_model': resolved,
                       'models_used': sorted(mu)}))
    return out


def checks_table(rep):
    rows = ['| id | check | result |', '|---|---|---|']
    rows += [f'| {c["id"]} | {c["check"]} | {c["result"]} |' for c in rep['checks']]
    return '\n'.join(rows)
