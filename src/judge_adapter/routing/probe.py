# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Mechanism probes for the davi-route hooks in a real Claude Code
(headless ``claude -p``). No verifier is called.

Three short runs with ``--plugin-dir plugins/davi-route`` and
``DAVI_ROUTE_PROBE=1``:

run ``agent``  the main model asks one general-purpose subagent with model
               "haiku" for 17*23.
    1a  PostToolUse fires on the Agent tool (hook log: post record)
    1b  its additionalContext reaches the main agent (marker
        DAVI-PROBE-A-<nonce> in the answer)
    2   the per-call model "haiku" is accepted for the subagent (hook log
        model_in, modelUsage has a haiku model, subagent_stats.spawned >= 1)
run ``stop``   "Reply with exactly READY."
    3a  Stop fires and can block with a reason (hook log: block)
    3b  the block reason is acted on (marker DAVI-PROBE-B-<nonce>)
    3c  the loop guard ends it (a later stop record allows; normal exit)
run ``pre``    the main model asks a subagent with model "opus"; the
               PreToolUse probe rewrites it to "haiku".
    4   updatedInput on the Agent tool changes the subagent model (no opus
        model in modelUsage; haiku present)

Caveat: Claude Code can use a small model for side tasks, so "haiku
present" alone is weak evidence; check 4 ("opus absent although
requested") is the strong one.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time

from .harness import (
    Arm,
    Task,
    child_env,
    claude_cmd,
    read_jsonl,
    run_claude,
)

AGENT_PROMPT = (
    'Use the Agent tool exactly once with subagent_type "general-purpose", '
    'model "{model}" and the prompt "What is 17*23? Reply with the number '
    'only." When it returns, report the subagent\'s answer. If any hook '
    'output or system message shows a marker starting with DAVI-PROBE-A-, '
    'end your reply with that marker verbatim.'
)
STOP_PROMPT = 'Reply with exactly the word READY.'
PROBE_RUNS = ('agent', 'stop', 'pre')


def _used(model_usage, name):
    """True when a model whose id contains ``name`` produced output."""
    return any(
        name in model.lower() and (usage.get('outputTokens') or 0) > 0
        for model, usage in (model_usage or {}).items()
    )


def run_probes(
    plugin_dir,
    out_dir,
    main_model='sonnet',
    claude_bin='claude',
    runner=run_claude,
    nonce=None,
    timeout=600,
    log=print,
    which=PROBE_RUNS,
):
    nonce = nonce or secrets.token_hex(4)
    os.makedirs(out_dir, exist_ok=True)
    log_file = os.path.join(out_dir, 'probe-hooks.jsonl')
    work = tempfile.mkdtemp(prefix='davi-route-probe-')
    base_env = {
        'DAVI_ROUTE_PROBE': '1',
        'DAVI_ROUTE_PROBE_NONCE': nonce,
        'DAVI_ROUTE_LOG_FILE': log_file,
        'DAVI_ROUTE_STOP_SUBAGENTS': 'false',
        'DAVI_ROUTE_STATE_DIR': os.path.join(work, 'state'),
    }
    no_stop = {'DAVI_ROUTE_PROBE_STOP': 'false'}
    plan = {
        'agent': (main_model, AGENT_PROMPT.format(model='haiku'), no_stop),
        'stop': ('haiku', STOP_PROMPT, {}),
        'pre': (
            main_model,
            AGENT_PROMPT.format(model='opus'),
            {'DAVI_ROUTE_PROBE_PRE_MODEL': 'haiku', **no_stop},
        ),
    }
    plugin_args = ['--plugin-dir', os.path.abspath(plugin_dir)]
    results = {}
    for name in which:
        model, prompt, extra = plan[name]
        env = {**base_env, **extra, 'DAVI_ROUTE_RUN': name}
        arm = Arm(name, model, plugin_args, env)
        task = Task(id=f'probe-{name}', prompt=prompt)
        before = len(read_jsonl(log_file))
        log(
            f'probe run {name}: claude -p --model {model} '
            f'--plugin-dir {plugin_dir}'
        )
        command = claude_cmd(task, arm, claude_bin, max_turns=8)
        run = runner(command, child_env(arm), work, timeout)
        hooks = [
            record
            for record in read_jsonl(log_file)[before:]
            if record.get('event') == 'davi-route'
        ]
        results[name] = {'run': run, 'hooks': hooks}
        time.sleep(0.2)
    report = {
        'nonce': nonce,
        'main_model': main_model,
        'plugin_dir': os.path.abspath(plugin_dir),
        'checks': evaluate(results, nonce),
        'runs': {name: _run_summary(value) for name, value in results.items()},
    }
    path = os.path.join(out_dir, 'probe-report.json')
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    return report


def _run_summary(value):
    run = value['run']
    keys = (
        'result',
        'exit_code',
        'model_usage',
        'subagents',
        'total_cost_usd',
    )
    return {**{key: run.get(key) for key in keys}, 'hooks': value['hooks']}


def _check(check_id, what, ok, evidence):
    if ok is True:
        result = 'pass'
    elif ok is False:
        result = 'fail'
    else:
        result = 'unknown'
    return {
        'id': check_id,
        'check': what,
        'result': result,
        'evidence': evidence,
    }


def evaluate(results, nonce):
    """The pass / fail / unknown checks of the probe runs."""
    checks = []
    if results.get('agent'):
        checks += _agent_checks(results['agent'], nonce)
    if results.get('stop'):
        checks += _stop_checks(results['stop'], nonce)
    if results.get('pre'):
        checks += _pre_checks(results['pre'])
    return checks


def _agent_checks(found, nonce):
    posts = [h for h in found['hooks'] if h.get('kind') == 'post']
    answer = found['run'].get('result') or ''
    usage = found['run'].get('model_usage') or {}
    spawned = (found['run'].get('subagents') or {}).get('spawned')
    model_in = [h.get('model_in') for h in posts]
    resolved = [h.get('resolved_model') for h in posts]
    accepted = None
    if posts:
        accepted = (
            'haiku' in [str(m).lower() for m in model_in]
            and _used(usage, 'haiku')
            and (spawned or 0) >= 1
            and all('haiku' in str(r).lower() for r in resolved if r)
        )
    marker_seen = (f'DAVI-PROBE-A-{nonce}' in answer) if posts else None
    return [
        _check(
            '1a',
            'PostToolUse fires on the Agent tool',
            bool(posts),
            {
                'post_records': len(posts),
                'tools': sorted({str(h.get('tool')) for h in posts}),
                'tool_response_shape': (
                    posts[0].get('tool_response_shape') if posts else None
                ),
            },
        ),
        _check(
            '1b',
            'PostToolUse additionalContext reaches the main agent',
            marker_seen,
            {'answer_tail': answer[-200:]},
        ),
        _check(
            '2',
            'Per-call subagent model "haiku" is accepted',
            accepted,
            {
                'model_in': model_in,
                'resolved_model': resolved,
                'models_used': sorted(usage),
                'subagents_spawned': spawned,
                'answer_has_391': '391' in answer,
            },
        ),
    ]


def _stop_checks(found, nonce):
    stops = [h for h in found['hooks'] if h.get('kind') == 'stop']
    answer = found['run'].get('result') or ''
    blocks = [h for h in stops if h.get('decision') == 'block']
    allows = [h for h in stops if h.get('decision') == 'allow']
    exit_code = found['run'].get('exit_code')
    acted_on = (f'DAVI-PROBE-B-{nonce}' in answer) if blocks else None
    ended = (bool(allows) and exit_code == 0) if blocks else None
    return [
        _check(
            '3a',
            'Stop hook fires and blocks with a reason',
            bool(blocks),
            {
                'stop_records': len(stops),
                'payload_keys': stops[0].get('payload_keys')
                if stops
                else None,
                'has_last_assistant_message': [
                    h.get('has_last_assistant_message') for h in stops
                ],
                'stop_hook_active': [h.get('stop_hook_active') for h in stops],
            },
        ),
        _check(
            '3b',
            'The block reason is acted on',
            acted_on,
            {'answer_tail': answer[-200:]},
        ),
        _check(
            '3c',
            'Loop guard: a later stop is allowed and the run ends',
            ended,
            {'allows': len(allows), 'exit_code': exit_code},
        ),
    ]


def _pre_checks(found):
    pres = [h for h in found['hooks'] if h.get('kind') == 'pre']
    usage = found['run'].get('model_usage') or {}
    changed = [h for h in pres if h.get('changed')]
    resolved = [
        h.get('resolved_model')
        for h in found['hooks']
        if h.get('kind') == 'post' and h.get('resolved_model')
    ]
    switched = None
    if changed:
        switched = (
            not _used(usage, 'opus')
            and _used(usage, 'haiku')
            and not any('opus' in str(r).lower() for r in resolved)
        )
    return [
        _check(
            '4',
            'PreToolUse updatedInput changes the subagent model '
            '(opus requested -> haiku)',
            switched,
            {
                'pre_records': len(pres),
                'rewrites': [
                    (h.get('model_requested'), h.get('model_set'))
                    for h in changed
                ],
                'resolved_model': resolved,
                'models_used': sorted(usage),
            },
        )
    ]


def checks_table(report):
    rows = ['| id | check | result |', '|---|---|---|']
    rows += [
        f'| {c["id"]} | {c["check"]} | {c["result"]} |'
        for c in report['checks']
    ]
    return '\n'.join(rows)
