# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Headless measurement for routing (C), plus readers for the in-Claude-Code hooks (A, B) and their probes.

Pipeline (each step caches to JSONL in one directory and skips what is already there):

1. ``generate``  run every task with ``claude -p --output-format json`` once per ARM (e.g. cheap=haiku,
                 strong=opus, or an arm that loads the davi-route plugin). Records the answer, total_cost_usd,
                 per-model usage (modelUsage), subagent stats, duration, and the grade.
2. ``judge``     score the answers of one arm (the cheap one) with each verifier: P(accept), cost, latency.
3. ``analyze``   offline, from the cache: always-cheap / always-strong / oracle / random-at-matched-rate baselines,
                 per-judge threshold sweep (accuracy vs cost incl. judge cost), AUROC of P(accept) for "cheap answer is
                 correct", and a FROZEN threshold chosen on a calibration split and reported on the held-out split.
                 Also a per-arm summary (accuracy, cost per task, cost per success) for end-to-end arms such as A/B.

Demo helpers (fixed threshold, no calibration split; for showing the flow, not for claims):

- ``regrade``  re-apply the task graders to cached runs (after a grader fix, or once an LLM-grader key is set).
- ``curate``   pick a DEMO subset where one judge ("ours") routes correctly and the others do not. This is a selection
               on outcomes: its numbers describe the selected tasks only and must be presented as selected cases.
- ``demo``     per task, each judge's P(accept) -> route -> final answer correct -> cost, then a summary (accuracy,
               strong-model call rate, total cost incl. judge cost) next to always-cheap and always-strong.

Escalation in the offline replay = cheap run + judge + a fresh strong run (the strong answer replaces the cheap one).
That is exactly what the live loop does with ``--model <next_model>``, so the replay is the live loop's cost, without
paying for it once per threshold.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import verifiers as V

RUNS, JUDGMENTS = 'runs.jsonl', 'judgments.jsonl'
GRADERS = ('contains', 'exact', 'regex', 'number', 'cmd', 'llm', 'none')


# --- tasks and arms --------------------------------------------------------------------------------------------------
@dataclass
class Task:
    id: str
    prompt: str
    answer: str | None = None
    grader: str | None = None          # default: 'contains' if answer else 'none'
    check_cmd: str | None = None       # grader 'cmd': shell command, the answer on stdin, exit 0 = correct
    cwd: str | None = None
    rubric: str | None = None
    max_turns: int | None = None

    @property
    def grader_name(self):
        return self.grader or ('cmd' if self.check_cmd else ('contains' if self.answer else 'none'))


def load_tasks(path):
    tasks, seen = [], set()
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            d = json.loads(line)
            t = Task(**{k: d.get(k) for k in Task.__dataclass_fields__})
            if not t.id or not t.prompt:
                raise ValueError(f'{path}:{n}: id and prompt are required')
            if t.id in seen:
                raise ValueError(f'{path}:{n}: duplicate id {t.id}')
            if t.grader_name not in GRADERS:
                raise ValueError(f'{path}:{n}: grader must be one of {GRADERS}')
            seen.add(t.id)
            tasks.append(t)
    return tasks


@dataclass
class Arm:
    name: str
    model: str
    args: list = field(default_factory=list)       # extra claude CLI args, e.g. ["--plugin-dir", "plugins/davi-route"]
    env: dict = field(default_factory=dict)        # extra environment, e.g. {"DAVI_ROUTE_ROUTE_ENABLED": "true"}


def parse_arm(spec):
    """'name=model' or 'name=model:extra args' (shell-split)."""
    name, _, rest = spec.partition('=')
    model, _, extra = rest.partition(':')
    if not name or not model:
        raise ValueError(f'arm must look like name=model[:extra args], got {spec!r}')
    return Arm(name.strip(), model.strip(), shlex.split(extra))


def load_arms(path):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    return [Arm(d['name'], d['model'], list(d.get('args', [])), dict(d.get('env', {}))) for d in data]


# --- cache ---------------------------------------------------------------------------------------------------------
_lock = threading.Lock()


def read_jsonl(path):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def append_jsonl(path, rec):
    with _lock:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str) + '\n')


def latest(records, key):
    """Last record per key (re-runs append; the newest wins)."""
    out = {}
    for r in records:
        out[key(r)] = r
    return out


# --- running claude -p -----------------------------------------------------------------------------------------------
ISOLATE_ENV_DROP = ('CLAUDE_CODE_SESSION_ID',)      # never let a nested run inherit the caller's session


def claude_cmd(task, arm, claude_bin='claude', max_turns=None, budget_usd=None, session_id=None):
    cmd = [claude_bin, '-p', task.prompt, '--model', arm.model, '--output-format', 'json',
           '--session-id', session_id or str(uuid.uuid4())]
    mt = task.max_turns or max_turns
    if mt:
        cmd += ['--max-turns', str(int(mt))]
    if budget_usd:
        cmd += ['--max-budget-usd', str(budget_usd)]
    return cmd + list(arm.args)


def parse_result(stdout):
    """The JSON result object of ``claude -p --output-format json`` (last JSON object line wins)."""
    for line in reversed((stdout or '').strip().splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                js = json.loads(line)
            except ValueError:
                continue
            if isinstance(js, dict) and ('result' in js or js.get('type') == 'result'):
                return js
    try:
        js = json.loads(stdout)
        return js if isinstance(js, dict) else None
    except (ValueError, TypeError):
        return None


def summarize_model_usage(mu):
    out = {}
    for m, u in (mu or {}).items():
        if isinstance(u, dict):
            out[m] = {k: u.get(k) for k in ('inputTokens', 'outputTokens', 'cacheReadInputTokens',
                                             'cacheCreationInputTokens', 'costUSD')}
    return out


def run_claude(cmd, env, cwd=None, timeout=900):
    t0 = time.time()
    try:
        # stdin closed: claude -p also reads piped stdin, and an inherited open pipe can block it until the timeout
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = 'timeout', e.stdout or '', e.stderr or ''
        out = out.decode() if isinstance(out, bytes) else out
        err = err.decode() if isinstance(err, bytes) else err
    js = parse_result(out) or {}
    sub = js.get('subagent_stats') if isinstance(js.get('subagent_stats'), dict) else {}
    return {'exit_code': code, 'wall_s': round(time.time() - t0, 3), 'result': js.get('result'),
            'is_error': js.get('is_error', code != 0), 'subtype': js.get('subtype'),
            'total_cost_usd': js.get('total_cost_usd'), 'duration_ms': js.get('duration_ms'),
            'num_turns': js.get('num_turns'), 'session_id': js.get('session_id'),
            'model_usage': summarize_model_usage(js.get('modelUsage')),
            'subagents': {'spawned': sub.get('spawned'), 'by_type': sub.get('by_type')} if sub else None,
            'permission_denials': len(js.get('permission_denials') or []),
            'stderr_tail': (err or '')[-500:] if code != 0 else None}


# --- grading ---------------------------------------------------------------------------------------------------------
def _norm(s):
    return ' '.join(str(s).casefold().split())


_NUM_RE = re.compile(r'-?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?')


def _last_number(s):
    nums = _NUM_RE.findall(s or '')
    return float(nums[-1].replace(',', '')) if nums else None


_MD_EMPH_RE = re.compile(r'\*\*|__|`')
_KO_UNITS = {'조': 10 ** 12, '억': 10 ** 8, '만': 10 ** 4}
_KO_AMOUNT_RE = re.compile(r'(?:\d[\d,]*(?:\.\d+)?\s*[조억만]\s*)+(?:\d[\d,]*(?:\.\d+)?)?')
_KO_PART_RE = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*([조억만]?)')


def _plain(s):
    """The answer text without markdown emphasis (``**B**`` -> ``B``), so graders see what the model meant."""
    return _MD_EMPH_RE.sub('', str(s))


def _last_ko_amount(s):
    """The last Korean-unit amount in ``s`` ('21억 3,000만' -> 2130000000.0), or None."""
    found = _KO_AMOUNT_RE.findall(s or '')
    if not found:
        return None
    total = 0.0
    for num, unit in _KO_PART_RE.findall(found[-1]):
        total += float(num.replace(',', '')) * _KO_UNITS.get(unit, 1)
    return total


LLM_GRADER_PROMPT = (
    'Decide whether the response gives the same final answer as the reference answer. Ignore formatting, explanation '
    'and wording; judge only the final answer. Reply with exactly "yes" or "no".\n\n<question>\n{q}\n</question>\n\n'
    '<reference>\n{a}\n</reference>\n\n<response>\n{r}\n</response>')


GRADER_DEFAULT_MODEL = 'claude-sonnet-5-5'      # grading defines the labels: a stronger model than the judges
_YES_NO_RE = re.compile(r'\b(yes|no)\b')


def llm_grade(task, result, env, transport=None, model=None):
    """LLM grading against the reference answer. -> (bool | None, detail)."""
    import httpx
    key = V.lazy_key(env, V.SECRET_ENVS['claude'])()
    model = model or V.opt(env, 'grader_model', GRADER_DEFAULT_MODEL)
    body = {'model': model, 'messages': [{'role': 'user', 'content': LLM_GRADER_PROMPT.format(
        q=V.clip(task.prompt, 8000), a=task.answer, r=V.clip(result, 16000))}]}
    if V.uses_effort(model):
        body.update(max_tokens=2048, output_config={'effort': 'low'})
    else:
        body.update(max_tokens=4, temperature=0.0)
    tr = transport or httpx.HTTPTransport(retries=0)
    with httpx.Client(transport=tr, timeout=120) as c:
        r = c.post(V.opt(env, 'claude_base_url', V.CLAUDE_BASE_URL) + '/v1/messages', json=body,
                   headers={'x-api-key': key, 'anthropic-version': V.ANTHROPIC_VERSION})
    if r.status_code != 200:
        return None, f'grader HTTP {r.status_code}'
    text = ''.join(b.get('text', '') for b in r.json().get('content') or []).strip().lower()
    found = _YES_NO_RE.findall(text)          # the last yes/no: a reason may come first
    if found:
        return found[-1] == 'yes', f'llm:{found[-1]}'
    return None, f'llm:unparsed {text[:20]!r}'


def grade(task, result, env=None, transport=None):
    """-> (correct: bool | None, detail). None = ungraded (no reference, grader failure, empty answer is False)."""
    g = task.grader_name
    if g == 'none':
        return None, 'ungraded'
    if result is None or not str(result).strip():
        return False, 'empty'
    if g == 'cmd':
        try:
            p = subprocess.run(task.check_cmd, shell=True, input=str(result), capture_output=True, text=True,
                               cwd=task.cwd, timeout=300)
            return p.returncode == 0, f'cmd exit {p.returncode}'
        except subprocess.TimeoutExpired:
            return None, 'cmd timeout'
    if task.answer is None:
        return None, 'no answer'
    text = _plain(result)
    if g == 'exact':
        return _norm(text) == _norm(task.answer), 'exact'
    if g == 'contains':
        return _norm(task.answer) in _norm(text), 'contains'
    if g == 'regex':
        return re.search(task.answer, text, re.I | re.S) is not None, 'regex'
    if g == 'number':
        want = _last_number(str(task.answer))
        got = [x for x in (_last_number(text), _last_ko_amount(text)) if x is not None]
        if not got or want is None:
            return False, 'number missing'
        return any(abs(x - want) <= max(1e-9, 1e-6 * abs(want)) for x in got), 'number'
    try:
        return llm_grade(task, str(result), env or os.environ, transport)
    except Exception as e:  # noqa: BLE001
        return None, f'llm grader error {type(e).__name__}'


# --- steps -----------------------------------------------------------------------------------------------------------
def child_env(arm, base=None):
    env = dict(os.environ if base is None else base)
    for k in ISOLATE_ENV_DROP:
        env.pop(k, None)
    env.update({k: str(v) for k, v in arm.env.items()})
    return env


def generate(tasks, arms, cache, workers=2, runner=run_claude, claude_bin='claude', max_turns=None, budget_usd=None,
             timeout=900, redo_errors=False, log=print, grade_env=None):
    path = os.path.join(cache, RUNS)
    done = latest(read_jsonl(path), lambda r: (r['task_id'], r['arm']))
    jobs = []
    for arm in arms:
        for t in tasks:
            prev = done.get((t.id, arm.name))
            if prev and not (redo_errors and prev.get('is_error')):
                continue
            jobs.append((t, arm))
    log(f'generate: {len(jobs)} runs to do ({len(done)} cached)')

    def one(job):
        t, arm = job
        cmd = claude_cmd(t, arm, claude_bin, max_turns, budget_usd)
        r = runner(cmd, child_env(arm), t.cwd, timeout)
        ok, why = grade(t, r.get('result'), env=grade_env) if not r.get('is_error') else (False, 'run_error')
        rec = {'task_id': t.id, 'arm': arm.name, 'model': arm.model, 'arm_args': arm.args,
               'arm_env_keys': sorted(arm.env), 'correct': ok, 'grade': why, 'ts': time.time(), **r}
        append_jsonl(path, rec)
        log(f'  {t.id} [{arm.name}] correct={ok} cost={r.get("total_cost_usd")} ({r.get("wall_s")}s)')
        return rec
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(one, jobs))


PRE_SUFFIX = ':pre'


def judged_name(judge, kind):
    """Cache name of a judge's results: ``davi`` for answers, ``davi:pre`` for task-only pre-routing, ..."""
    return judge if kind == 'answer' else f'{judge}:{kind}'


def is_pre(judge_name):
    """Pre-routing results: an escalated task goes to the strong model directly (no cheap run is paid for)."""
    return judge_name.endswith(PRE_SUFFIX)


def judge(tasks, cache, source_arm, judges, env=None, workers=4, verifier_factory=None, kind='answer', log=print):
    """Score ``source_arm``'s answers (``kind`` answer/completion), or, for a task-only kind (``pre``), the tasks
    themselves; results are stored under ``source_arm`` so they line up with that arm's runs."""
    env = dict(os.environ if env is None else env)
    runs = latest(read_jsonl(os.path.join(cache, RUNS)), lambda r: (r['task_id'], r['arm']))
    jpath = os.path.join(cache, JUDGMENTS)
    have = latest(read_jsonl(jpath), lambda r: (r['task_id'], r['arm'], r['judge']))
    by_id = {t.id: t for t in tasks}
    factory = verifier_factory or (lambda name: V.build_verifier(name, env))
    task_only = kind in V.TASK_ONLY_KINDS
    out = []
    for judge_name in judges:
        name = judged_name(judge_name, kind)
        ver = factory(judge_name)
        todo = [(tid, r) for (tid, arm), r in runs.items() if arm == source_arm and tid in by_id
                and (tid, arm, name) not in have and (task_only or r.get('result') is not None)]
        log(f'judge {name}: {len(todo)} {"tasks" if task_only else "answers"} to score')

        def one(item, ver=ver, name=name):
            tid, r = item
            t = by_id[tid]
            v = ver.verify(t.prompt, None if task_only else r['result'], rubric=t.rubric, kind=kind)
            rec = {'task_id': tid, 'arm': source_arm, 'judge': name, 'kind': kind, 'ts': time.time(), **v.to_dict()}
            rec['judge'] = name
            append_jsonl(jpath, rec)
            return rec
        try:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                out += list(ex.map(one, todo))
        finally:
            ver.close()
    return out


# --- analysis --------------------------------------------------------------------------------------------------------
def auroc(scores, labels):
    """P(score of a positive > score of a negative), ties count half. None if a class is empty."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    pairs = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg])
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (i + j + 1) / 2.0
        rank_sum += avg * sum(1 for k in range(i, j) if pairs[k][1] == 1)
        i = j
    n1, n0 = len(pos), len(neg)
    return (rank_sum - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def split_ids(ids, calib_frac, seed):
    """Deterministic split by hashing (seed, id)."""
    calib, test = [], []
    for i in ids:
        h = int(hashlib.sha256(f'{seed}|{i}'.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        (calib if h < calib_frac else test).append(i)
    return calib, test


def _point(rows, thr, on_error='accept'):
    """Replay a threshold over rows: escalate when p < thr (p None -> on_error). A pre-routing row (``pre``) that
    escalates pays judge + strong only: the cheap model never ran."""
    acc = cost = esc = 0.0
    for r in rows:
        p = r['p']
        e = (on_error == 'escalate') if p is None else p < thr
        esc += e
        acc += r['s'] if e else r['c']
        cost += (0.0 if (e and r.get('pre')) else r['cc']) + (r['jc'] or 0.0) + (r['sc'] if e else 0.0)
    n = len(rows)
    return {'threshold': thr, 'escalation_rate': esc / n, 'accuracy': acc / n, 'cost_per_task': cost / n}


def _random_point(rows, rate):
    n = len(rows)
    acc = sum((1 - rate) * r['c'] + rate * r['s'] for r in rows) / n
    cost = sum(r['cc'] + rate * r['sc'] for r in rows) / n
    return {'escalation_rate': rate, 'accuracy': acc, 'cost_per_task': cost}


def _baselines(rows):
    n = len(rows)
    return {
        'always_cheap': {'accuracy': sum(r['c'] for r in rows) / n, 'cost_per_task': sum(r['cc'] for r in rows) / n,
                         'escalation_rate': 0.0},
        'always_strong': {'accuracy': sum(r['s'] for r in rows) / n, 'cost_per_task': sum(r['sc'] for r in rows) / n,
                          'escalation_rate': 1.0},
        'oracle': {'accuracy': sum(max(r['c'], r['s']) for r in rows) / n,
                   'cost_per_task': sum(r['cc'] + (0 if r['c'] else r['sc']) for r in rows) / n,
                   'escalation_rate': sum(1 for r in rows if not r['c']) / n},
    }


def sweep(rows, on_error='accept'):
    ps = sorted({r['p'] for r in rows if r['p'] is not None})
    thrs = [0.0] + [p + 1e-12 for p in ps]        # each threshold escalates every p strictly below it
    return [_point(rows, t, on_error) for t in thrs]


def choose_threshold(rows, tolerance, on_error='accept'):
    """Cheapest threshold whose accuracy is within ``tolerance`` of always-strong on these rows; else the most
    accurate one (ties -> cheaper)."""
    strong = _baselines(rows)['always_strong']['accuracy']
    pts = sweep(rows, on_error)
    ok = [p for p in pts if p['accuracy'] >= strong - tolerance - 1e-12]
    if ok:
        return min(ok, key=lambda p: (p['cost_per_task'], -p['accuracy']))['threshold'], 'within_tolerance'
    return max(pts, key=lambda p: (p['accuracy'], -p['cost_per_task']))['threshold'], 'max_accuracy'


def arm_summary(runs):
    by = {}
    for r in runs.values():
        by.setdefault(r['arm'], []).append(r)
    out = {}
    for arm, rs in sorted(by.items()):
        graded = [r for r in rs if r.get('correct') is not None]
        costs = [r['total_cost_usd'] for r in rs if isinstance(r.get('total_cost_usd'), (int, float))]
        succ = sum(1 for r in graded if r['correct'])
        out[arm] = {'n': len(rs), 'graded': len(graded),
                    'accuracy': succ / len(graded) if graded else None,
                    'cost_per_task': _mean(costs), 'total_cost': sum(costs),
                    'cost_per_success': (sum(costs) / succ) if succ else None,
                    'mean_wall_s': _mean(r['wall_s'] for r in rs if isinstance(r.get('wall_s'), (int, float))),
                    'errors': sum(1 for r in rs if r.get('is_error')),
                    'subagents_spawned': sum((r.get('subagents') or {}).get('spawned') or 0 for r in rs),
                    'models_seen': sorted({m for r in rs for m in (r.get('model_usage') or {})})}
    return out


def analyze(cache, cheap, strong, calib_frac=0.3, seed=0, tolerance=0.02, on_error='accept', out_dir=None):
    runs = latest(read_jsonl(os.path.join(cache, RUNS)), lambda r: (r['task_id'], r['arm']))
    js = latest(read_jsonl(os.path.join(cache, JUDGMENTS)), lambda r: (r['task_id'], r['arm'], r['judge']))
    report = {'arms': arm_summary(runs), 'cheap_arm': cheap, 'strong_arm': strong, 'tolerance': tolerance,
              'calib_frac': calib_frac, 'seed': seed, 'judges': {}, 'notes': []}
    base = {}
    for (tid, arm), r in runs.items():
        if arm != cheap:
            continue
        s = runs.get((tid, strong))
        if not s or r.get('correct') is None or s.get('correct') is None:
            continue
        if not isinstance(r.get('total_cost_usd'), (int, float)) or not isinstance(s.get('total_cost_usd'),
                                                                                    (int, float)):
            continue
        base[tid] = {'id': tid, 'c': bool(r['correct']), 's': bool(s['correct']), 'cc': r['total_cost_usd'],
                     'sc': s['total_cost_usd']}
    report['n_paired'] = len(base)
    if not base:
        report['notes'].append('no task has graded runs with known cost in both the cheap and the strong arm')
        _write(report, [], out_dir)
        return report
    rows_all = list(base.values())
    report['baselines'] = _baselines(rows_all)
    calib_ids, test_ids = split_ids(sorted(base), calib_frac, seed)
    curve_rows = []
    for judge_name in sorted({j for (_, a, j) in js if a == cheap}):
        rows = []
        for tid, b in base.items():
            j = js.get((tid, cheap, judge_name))
            if j is None:
                continue
            rows.append({**b, 'p': j.get('p_accept'), 'jc': j.get('cost_usd'), 'lat': j.get('latency_s'),
                         'pre': is_pre(judge_name)})
        if not rows:
            continue
        failed = sum(1 for r in rows if r['p'] is None)
        unknown_cost = sum(1 for r in rows if r['jc'] is None and r['p'] is not None)
        scored = [r for r in rows if r['p'] is not None]
        pts = sweep(rows, on_error)
        for p in pts:
            rnd = _random_point(rows, p['escalation_rate'])
            curve_rows.append({'judge': judge_name, **p, 'random_accuracy': rnd['accuracy'],
                               'random_cost_per_task': rnd['cost_per_task']})
        jr = {'n': len(rows), 'verifier_failures': failed, 'unknown_judge_cost': unknown_cost,
              'auroc_cheap_correct': auroc([r['p'] for r in scored], [r['c'] for r in scored]),
              'mean_judge_cost': _mean(r['jc'] for r in rows if r['jc'] is not None),
              'mean_judge_latency_s': _mean(r['lat'] for r in rows if isinstance(r.get('lat'), (int, float))),
              'baselines': _baselines(rows)}
        cal = [r for r in rows if r['id'] in set(calib_ids)]
        tst = [r for r in rows if r['id'] in set(test_ids)]
        if cal and tst:
            thr, how = choose_threshold(cal, tolerance, on_error)
            pt = _point(tst, thr, on_error)
            b = _baselines(tst)
            rnd = _random_point(tst, pt['escalation_rate'])
            gap = b['always_strong']['accuracy'] - b['always_cheap']['accuracy']
            jr['frozen'] = {
                'threshold': thr, 'chosen_by': how, 'n_calib': len(cal), 'n_test': len(tst), 'test': pt,
                'test_baselines': b, 'test_random_same_rate': rnd,
                'accuracy_minus_strong': pt['accuracy'] - b['always_strong']['accuracy'],
                'accuracy_minus_random': pt['accuracy'] - rnd['accuracy'],
                'cost_vs_strong': (pt['cost_per_task'] / b['always_strong']['cost_per_task']
                                   if b['always_strong']['cost_per_task'] else None),
                'gap_closed': ((pt['accuracy'] - b['always_cheap']['accuracy']) / gap) if gap > 0 else None}
        else:
            jr['frozen'] = None
        report['judges'][judge_name] = jr
    if len(base) < 100:
        report['notes'].append(f'only {len(base)} paired tasks: differences of a few points are within noise')
    _write(report, curve_rows, out_dir)
    return report


def _fmt(x, pct=False, money=False):
    if x is None:
        return '-'
    if pct:
        return f'{100 * x:.1f}%'
    if money:
        return f'${x:.5f}'
    return f'{x:.3f}' if isinstance(x, float) else str(x)


def report_markdown(rep):
    L = ['# Routing report', '', f'cheap arm `{rep["cheap_arm"]}`, strong arm `{rep["strong_arm"]}`, '
         f'paired tasks {rep.get("n_paired", 0)}', '']
    L += ['## Arms (end-to-end)', '', '| arm | n | accuracy | cost/task | cost/success | wall s | subagents | models |',
          '|---|---|---|---|---|---|---|---|']
    for a, s in rep['arms'].items():
        L.append(f'| {a} | {s["n"]} | {_fmt(s["accuracy"], pct=True)} | {_fmt(s["cost_per_task"], money=True)} | '
                 f'{_fmt(s["cost_per_success"], money=True)} | {_fmt(s["mean_wall_s"])} | {s["subagents_spawned"]} | '
                 f'{", ".join(s["models_seen"])} |')
    if rep.get('baselines'):
        L += ['', '## Baselines (all paired tasks)', '', '| policy | accuracy | cost/task | escalation |',
              '|---|---|---|---|']
        for k, b in rep['baselines'].items():
            L.append(f'| {k} | {_fmt(b["accuracy"], pct=True)} | {_fmt(b["cost_per_task"], money=True)} | '
                     f'{_fmt(b["escalation_rate"], pct=True)} |')
    if rep['judges']:
        L += ['', f'## Judges: frozen threshold (calibration {rep["calib_frac"]:.0%}, tolerance '
              f'{rep["tolerance"]:.0%} vs always-strong), held-out split', '',
              '| judge | AUROC | thr | test acc | strong acc | random acc (same rate) | escalation | cost/task | '
              'cost vs strong | gap closed | judge $/call | judge s |', '|---|---|---|---|---|---|---|---|---|---|---|---|']
        for j, r in rep['judges'].items():
            f = r.get('frozen') or {}
            t = f.get('test') or {}
            tb = (f.get('test_baselines') or {}).get('always_strong') or {}
            L.append(f'| {j} | {_fmt(r["auroc_cheap_correct"])} | {_fmt(f.get("threshold"))} | '
                     f'{_fmt(t.get("accuracy"), pct=True)} | {_fmt(tb.get("accuracy"), pct=True)} | '
                     f'{_fmt((f.get("test_random_same_rate") or {}).get("accuracy"), pct=True)} | '
                     f'{_fmt(t.get("escalation_rate"), pct=True)} | {_fmt(t.get("cost_per_task"), money=True)} | '
                     f'{_fmt(f.get("cost_vs_strong"), pct=True)} | {_fmt(f.get("gap_closed"), pct=True)} | '
                     f'{_fmt(r["mean_judge_cost"], money=True)} | {_fmt(r["mean_judge_latency_s"])} |')
        L += ['', 'Failures: ' + ', '.join(f'{j} {r["verifier_failures"]}/{r["n"]}' for j, r in rep['judges'].items())]
    if rep['notes']:
        L += ['', '## Notes', ''] + [f'- {n}' for n in rep['notes']]
    L += ['', 'Escalated tasks are charged cheap run + judge + strong run. Costs are `total_cost_usd` from Claude Code '
          '(list-price estimates) and judge estimates; not bills.']
    return '\n'.join(L) + '\n'


def _write(report, curve_rows, out_dir):
    if not out_dir:
        return
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(out_dir, 'report.md'), 'w', encoding='utf-8') as f:
        f.write(report_markdown(report))
    with open(os.path.join(out_dir, 'curve.csv'), 'w', newline='', encoding='utf-8') as f:
        cols = ['judge', 'threshold', 'escalation_rate', 'accuracy', 'cost_per_task', 'random_accuracy',
                'random_cost_per_task']
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in curve_rows:
            w.writerow({k: r.get(k) for k in cols})


# --- demo helpers: regrade, curate, demo -------------------------------------------------------------------------------
def regrade(tasks, cache, env=None, transport=None, log=print):
    """Re-apply each task's grader to its cached runs; appends a record only when the grade changes."""
    path = os.path.join(cache, RUNS)
    by_id = {t.id: t for t in tasks}
    changed = 0
    for (tid, _arm), r in latest(read_jsonl(path), lambda r: (r['task_id'], r['arm'])).items():
        t = by_id.get(tid)
        if t is None or r.get('is_error'):
            continue
        ok, why = grade(t, r.get('result'), env=env, transport=transport)
        if (ok, why) != (r.get('correct'), r.get('grade')):
            append_jsonl(path, {**r, 'correct': ok, 'grade': why, 'regraded_from': r.get('correct'), 'ts': time.time()})
            changed += 1
    log(f'regrade: {changed} runs changed')
    return changed


def ideal_route(cheap_ok, strong_ok):
    """The route that is right in hindsight: keep a correct cheap answer, escalate a wrong one the strong model fixes.
    None when both are wrong (no route changes the outcome)."""
    if cheap_ok:
        return 'accept'
    return 'escalate' if strong_ok else None


def route_of(p, threshold, on_error='accept'):
    return ('escalate' if on_error == 'escalate' else 'accept') if p is None else (
        'escalate' if p < threshold else 'accept')


def _last_line(text, width=60):
    lines = [x.strip() for x in str(text or '').strip().splitlines() if x.strip()]
    s = lines[-1] if lines else ''
    return s if len(s) <= width else s[:width - 1] + '…'


def demo_rows(cache, cheap, strong, threshold=0.5, on_error='accept', task_ids=None):
    """One row per task that has graded cheap and strong runs: each judge's P -> route -> final correct -> cost.
    A judge's missing judgment shows as None (that task is still listed)."""
    runs = latest(read_jsonl(os.path.join(cache, RUNS)), lambda r: (r['task_id'], r['arm']))
    js = latest(read_jsonl(os.path.join(cache, JUDGMENTS)), lambda r: (r['task_id'], r['arm'], r['judge']))
    judges = sorted({j for (_, a, j) in js if a == cheap})
    want = None if task_ids is None else list(task_ids)
    ids = want if want is not None else sorted({t for (t, a) in runs if a == cheap})
    rows = []
    for tid in ids:
        c, s = runs.get((tid, cheap)), runs.get((tid, strong))
        if not c or not s or c.get('correct') is None or s.get('correct') is None:
            continue
        cc, sc = c.get('total_cost_usd') or 0.0, s.get('total_cost_usd') or 0.0
        row = {'id': tid, 'cheap_ok': bool(c['correct']), 'strong_ok': bool(s['correct']), 'cheap_cost': cc,
               'strong_cost': sc, 'cheap_answer': _last_line(c.get('result')),
               'strong_answer': _last_line(s.get('result')), 'ideal': ideal_route(c['correct'], s['correct']),
               'judges': {}}
        for j in judges:
            v = js.get((tid, cheap, j))
            if v is None:
                row['judges'][j] = None
                continue
            p = v.get('p_accept')
            route = route_of(p, threshold, on_error)
            esc = route == 'escalate'
            jc = v.get('cost_usd')
            first = 0.0 if (esc and is_pre(j)) else cc          # pre-routing: an escalated task skips the cheap run
            row['judges'][j] = {'p': p, 'route': route, 'correct': row['strong_ok'] if esc else row['cheap_ok'],
                                'judge_cost': jc, 'cost': first + (jc or 0.0) + (sc if esc else 0.0),
                                'right_route': row['ideal'] is None or route == row['ideal']}
        rows.append(row)
    return rows, judges


def demo_summary(rows, judges):
    n = len(rows)
    if not n:
        return {}
    out = {'always_cheap': {'accuracy': sum(r['cheap_ok'] for r in rows) / n, 'strong_rate': 0.0,
                            'total_cost': sum(r['cheap_cost'] for r in rows), 'judge_cost': 0.0, 'n': n},
           'always_strong': {'accuracy': sum(r['strong_ok'] for r in rows) / n, 'strong_rate': 1.0,
                             'total_cost': sum(r['strong_cost'] for r in rows), 'judge_cost': 0.0, 'n': n}}
    for j in judges:
        got = [r['judges'][j] for r in rows if r['judges'].get(j)]
        if not got:
            continue
        m = len(got)
        out[j] = {'accuracy': sum(g['correct'] for g in got) / m,
                  'strong_rate': sum(g['route'] == 'escalate' for g in got) / m,
                  'total_cost': sum(g['cost'] for g in got),
                  'judge_cost': sum(g['judge_cost'] or 0.0 for g in got),
                  'unknown_judge_cost': sum(g['judge_cost'] is None for g in got),
                  'right_route': sum(g['right_route'] for g in got) / m, 'n': m}
    return out


def curate(cache, cheap, strong, ours='davi', n=20, accept_frac=0.5, threshold=0.5, min_margin=0.0,
           require='all', on_error='accept'):
    """Pick up to ``n`` tasks where ``ours`` takes the right route and the other judges do not (``require='all'``: every
    other judge is wrong; ``'any'``: at least one). ``accept_frac`` of them are tasks whose right route is accept (the
    cheap answer was correct), the rest escalate tasks; a short side is filled from the other. Within a side, tasks
    where more rivals are wrong and ``ours`` is farther from the threshold come first. Both-wrong tasks are skipped."""
    rows, judges = demo_rows(cache, cheap, strong, threshold, on_error)
    if ours not in judges:
        raise ValueError(f'no judgments by {ours!r} in {cache} (have {judges})')
    rivals = [j for j in judges if j != ours]
    pools = {'accept': [], 'escalate': []}
    for r in rows:
        mine = r['judges'].get(ours)
        if r['ideal'] is None or not mine or not all(r['judges'].get(j) for j in rivals):
            continue
        if mine['route'] != r['ideal'] or mine['p'] is None or abs(mine['p'] - threshold) < min_margin:
            continue
        wrong = sum(r['judges'][j]['route'] != r['ideal'] for j in rivals)
        if wrong < (len(rivals) if require == 'all' else 1) or (rivals and wrong == 0):
            continue
        pools[r['ideal']].append((-wrong, -abs(mine['p'] - threshold), r['id']))
    for v in pools.values():
        v.sort()
    k_acc = min(len(pools['accept']), round(n * accept_frac))
    k_esc = min(len(pools['escalate']), n - k_acc)
    k_acc = min(len(pools['accept']), n - k_esc)
    picked = [x[2] for x in pools['accept'][:k_acc]] + [x[2] for x in pools['escalate'][:k_esc]]
    return {'selected': picked, 'ours': ours, 'rivals': rivals, 'threshold': threshold, 'require': require,
            'min_margin': min_margin, 'available': {k: len(v) for k, v in pools.items()},
            'picked': {'accept': k_acc, 'escalate': k_esc}, 'n_pool': len(rows),
            'note': 'selected on outcomes for a demo; not an estimate of general performance'}


def demo_text(rows, judges, threshold, width=36):
    L = [f'threshold {threshold}: escalate when P(accept) < threshold. O = final answer correct, X = wrong.', '']
    head = f'{"task":<{width}} {"haiku":<5} {"opus":<5} ' + ' '.join(f'{j:<22}' for j in judges)
    L += [head, '-' * len(head)]
    for r in rows:
        cells = []
        for j in judges:
            g = r['judges'].get(j)
            if not g:
                cells.append(f'{"-":<22}')
                continue
            p = '  -  ' if g['p'] is None else f'{g["p"]:.2f}'
            cells.append(f'{p} {g["route"]:<8} {"O" if g["correct"] else "X"} ${g["cost"]:.3f}'.ljust(22))
        L.append(f'{r["id"][:width]:<{width}} {"O" if r["cheap_ok"] else "X":<5} {"O" if r["strong_ok"] else "X":<5} '
                 + ' '.join(cells))
    s = demo_summary(rows, judges)
    if s:
        L += ['', f'{"policy":<14} {"n":>3} {"accuracy":>9} {"strong calls":>13} {"total cost":>11} {"judge cost":>11} '
              f'{"right route":>12}', '-' * 80]
        for k, v in s.items():
            rr = f'{100 * v["right_route"]:.0f}%' if 'right_route' in v else '-'
            jc = f'${v["judge_cost"]:.4f}' + ('*' if v.get('unknown_judge_cost') else '')
            L.append(f'{k:<14} {v["n"]:>3} {100 * v["accuracy"]:>8.0f}% {100 * v["strong_rate"]:>12.0f}% '
                     f'${v["total_cost"]:>10.4f} {jc:>11} {rr:>12}')
        if any(v.get('unknown_judge_cost') for v in s.values()):
            L.append('* some judge costs unknown (counted as 0)')
    L += ['', 'Costs: Claude Code total_cost_usd and judge estimates (list prices, not bills). Escalated = cheap + judge '
          '+ strong.']
    return '\n'.join(L) + '\n'


# --- hook logs (A / B inside Claude Code) --------------------------------------------------------------------------
def hooklog_summary(path):
    recs = [r for r in read_jsonl(path) if r.get('event') == 'davi-route']
    out = {'lines': len(recs), 'by_kind': {}}
    for kind in ('pre', 'post', 'stop', 'subagent_stop'):
        rs = [r for r in recs if r.get('kind') == kind]
        if not rs:
            continue
        ps = sorted(r['p_accept'] for r in rs if isinstance(r.get('p_accept'), (int, float)))
        k = {'n': len(rs), 'status': {}, 'decision': {}, 'emitted': sum(1 for r in rs if r.get('emitted'))}
        for r in rs:
            k['status'][r.get('status')] = k['status'].get(r.get('status'), 0) + 1
            if r.get('decision'):
                k['decision'][r['decision']] = k['decision'].get(r['decision'], 0) + 1
        if ps:
            k['p_accept'] = {'n': len(ps), 'min': ps[0], 'median': ps[len(ps) // 2], 'max': ps[-1]}
        costs = [r['cost_usd'] for r in rs if isinstance(r.get('cost_usd'), (int, float))]
        if costs:
            k['judge_cost_total'] = sum(costs)
        if kind == 'pre':
            k['model_set'] = {}
            for r in rs:
                if r.get('changed'):
                    key = f'{r.get("model_requested")} -> {r.get("model_set")} ({r.get("source")})'
                    k['model_set'][key] = k['model_set'].get(key, 0) + 1
        out['by_kind'][kind] = k
    return out
