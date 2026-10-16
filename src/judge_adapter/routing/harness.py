# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Headless measurement: run tasks with ``claude -p`` per arm, judge them,
keep everything in one cache directory.

1. ``generate``  run every task once per ARM (e.g. cheap=haiku, mid=sonnet,
                 strong=opus, or an arm that loads the davi-route plugin);
                 record the answer, cost, per-model usage and the grade.
2. ``judge``     judge one arm's answers (kind ``answer``) or the tasks
                 themselves (``pre``, ``tier``) with each verifier.
3. ``regrade``   re-apply the graders to cached runs.

The analysis of a cache lives in ``analysis``. Each step appends JSONL to
the cache and skips what is already there (the newest record wins).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .grading import GRADERS, grade
from .questions import TASK_ONLY_KINDS
from .verifiers import build_verifier

RUNS_FILE = 'runs.jsonl'
JUDGMENTS_FILE = 'judgments.jsonl'
# never let a nested run inherit the caller's session
ISOLATE_ENV_DROP = ('CLAUDE_CODE_SESSION_ID',)


# --- tasks and arms ---------------------------------------------------------
@dataclass
class Task:
    """One task of a JSONL task file."""

    id: str
    prompt: str
    answer: str | None = None
    grader: str | None = None  # default: 'contains' if answer else 'none'
    check_cmd: str | None = None  # 'cmd': the answer on stdin, exit 0 = ok
    cwd: str | None = None
    rubric: str | None = None
    max_turns: int | None = None

    @property
    def grader_name(self):
        if self.grader:
            return self.grader
        if self.check_cmd:
            return 'cmd'
        return 'contains' if self.answer else 'none'


_TASK_FIELDS = tuple(Task.__dataclass_fields__)


def load_tasks(path):
    """Tasks of a JSONL file; ids must be unique and graders known."""
    tasks, seen = [], set()
    with open(path, encoding='utf-8') as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            data = json.loads(line)
            task = Task(**{key: data.get(key) for key in _TASK_FIELDS})
            where = f'{path}:{number}'
            if not task.id or not task.prompt:
                raise ValueError(f'{where}: id and prompt are required')
            if task.id in seen:
                raise ValueError(f'{where}: duplicate id {task.id}')
            if task.grader_name not in GRADERS:
                raise ValueError(f'{where}: grader must be one of {GRADERS}')
            seen.add(task.id)
            tasks.append(task)
    return tasks


@dataclass
class Arm:
    """One way to run every task: a model plus extra CLI args and env."""

    name: str
    model: str
    args: list = field(default_factory=list)  # e.g. ["--plugin-dir", "..."]
    env: dict = field(default_factory=dict)  # e.g. {"DAVI_ROUTE_...": "1"}


def parse_arm(spec):
    """'name=model' or 'name=model:extra args' (shell-split)."""
    name, _, rest = spec.partition('=')
    model, _, extra = rest.partition(':')
    if not name or not model:
        raise ValueError(
            f'arm must look like name=model[:extra args], got {spec!r}'
        )
    return Arm(name.strip(), model.strip(), shlex.split(extra))


def load_arms(path):
    with open(path, encoding='utf-8') as handle:
        data = json.load(handle)
    return [
        Arm(
            item['name'],
            item['model'],
            list(item.get('args', [])),
            dict(item.get('env', {})),
        )
        for item in data
    ]


# --- cache ------------------------------------------------------------------
_APPEND_LOCK = threading.Lock()


def read_jsonl(path):
    """Every JSON line of ``path`` (missing file -> [], bad lines skipped)."""
    if not os.path.isfile(path):
        return []
    records = []
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
    return records


def append_jsonl(path, record):
    """Append one JSON line (thread-safe within the process)."""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    with _APPEND_LOCK:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(line + '\n')


def latest(records, key):
    """The last record per ``key(record)``: re-runs append, newest wins."""
    newest = {}
    for record in records:
        newest[key(record)] = record
    return newest


class RunCache:
    """The cache directory: model runs and judgments as JSONL."""

    def __init__(self, directory):
        self._directory = directory

    @property
    def directory(self):
        return self._directory

    @property
    def runs_path(self):
        return os.path.join(self._directory, RUNS_FILE)

    @property
    def judgments_path(self):
        return os.path.join(self._directory, JUDGMENTS_FILE)

    def runs(self):
        """{(task_id, arm): newest run record}."""
        return latest(
            read_jsonl(self.runs_path),
            lambda record: (record['task_id'], record['arm']),
        )

    def judgments(self):
        """{(task_id, arm, judge): newest judgment record}."""
        return latest(
            read_jsonl(self.judgments_path),
            lambda record: (record['task_id'], record['arm'], record['judge']),
        )

    def add_run(self, record):
        append_jsonl(self.runs_path, record)

    def add_judgment(self, record):
        append_jsonl(self.judgments_path, record)


# --- running claude -p ------------------------------------------------------
def claude_cmd(
    task,
    arm,
    claude_bin='claude',
    max_turns=None,
    budget_usd=None,
    session_id=None,
):
    """The ``claude -p`` command line for one task on one arm."""
    command = [
        claude_bin,
        '-p',
        task.prompt,
        '--model',
        arm.model,
        '--output-format',
        'json',
        '--session-id',
        session_id or str(uuid.uuid4()),
    ]
    turns = task.max_turns or max_turns
    if turns:
        command += ['--max-turns', str(int(turns))]
    if budget_usd:
        command += ['--max-budget-usd', str(budget_usd)]
    return command + list(arm.args)


def parse_result(stdout):
    """The result object of ``claude -p --output-format json``."""
    for line in reversed((stdout or '').strip().splitlines()):
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and (
            'result' in data or data.get('type') == 'result'
        ):
            return data
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def summarize_model_usage(model_usage):
    keys = (
        'inputTokens',
        'outputTokens',
        'cacheReadInputTokens',
        'cacheCreationInputTokens',
        'costUSD',
    )
    return {
        model: {key: usage.get(key) for key in keys}
        for model, usage in (model_usage or {}).items()
        if isinstance(usage, dict)
    }


def child_env(arm, base=None):
    """The environment of a nested run: no inherited session id."""
    env = dict(os.environ if base is None else base)
    for key in ISOLATE_ENV_DROP:
        env.pop(key, None)
    env.update({key: str(value) for key, value in arm.env.items()})
    return env


def run_claude(command, env, cwd=None, timeout=900):
    """Run one ``claude -p`` command and summarize its result."""
    started = time.time()
    try:
        # stdin closed: claude -p also reads piped stdin, and an inherited
        # open pipe can block it until the timeout
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        code, out, err = process.returncode, process.stdout, process.stderr
    except subprocess.TimeoutExpired as error:
        code = 'timeout'
        out = _decoded(error.stdout)
        err = _decoded(error.stderr)
    data = parse_result(out) or {}
    stats = data.get('subagent_stats')
    stats = stats if isinstance(stats, dict) else {}
    subagents = None
    if stats:
        subagents = {
            'spawned': stats.get('spawned'),
            'by_type': stats.get('by_type'),
        }
    return {
        'exit_code': code,
        'wall_s': round(time.time() - started, 3),
        'result': data.get('result'),
        'is_error': data.get('is_error', code != 0),
        'subtype': data.get('subtype'),
        'total_cost_usd': data.get('total_cost_usd'),
        'duration_ms': data.get('duration_ms'),
        'num_turns': data.get('num_turns'),
        'session_id': data.get('session_id'),
        'model_usage': summarize_model_usage(data.get('modelUsage')),
        'subagents': subagents,
        'permission_denials': len(data.get('permission_denials') or []),
        'stderr_tail': (err or '')[-500:] if code != 0 else None,
    }


def _decoded(stream):
    if isinstance(stream, bytes):
        return stream.decode()
    return stream or ''


# --- steps ------------------------------------------------------------------
def generate(
    tasks,
    arms,
    cache,
    workers=2,
    runner=run_claude,
    claude_bin='claude',
    max_turns=None,
    budget_usd=None,
    timeout=900,
    redo_errors=False,
    log=print,
    grade_env=None,
):
    """Run every task on every arm that is not cached yet."""
    store = RunCache(cache)
    done = store.runs()
    jobs = []
    for arm in arms:
        for task in tasks:
            previous = done.get((task.id, arm.name))
            if previous and not (redo_errors and previous.get('is_error')):
                continue
            jobs.append((task, arm))
    log(f'generate: {len(jobs)} runs to do ({len(done)} cached)')

    def run_one(job):
        task, arm = job
        command = claude_cmd(task, arm, claude_bin, max_turns, budget_usd)
        run = runner(command, child_env(arm), task.cwd, timeout)
        if run.get('is_error'):
            correct, why = False, 'run_error'
        else:
            correct, why = grade(task, run.get('result'), env=grade_env)
        record = {
            'task_id': task.id,
            'arm': arm.name,
            'model': arm.model,
            'arm_args': arm.args,
            'arm_env_keys': sorted(arm.env),
            'correct': correct,
            'grade': why,
            'ts': time.time(),
            **run,
        }
        store.add_run(record)
        log(
            f'  {task.id} [{arm.name}] correct={correct} '
            f'cost={run.get("total_cost_usd")} ({run.get("wall_s")}s)'
        )
        return record

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(run_one, jobs))


def judged_name(judge_name, kind):
    """Cache name of a judge's results: 'davi', 'davi:pre', 'davi:tier'."""
    return judge_name if kind == 'answer' else f'{judge_name}:{kind}'


def is_pre(judge_name):
    """Up-front routing: an escalated task goes straight to the strong
    model, so no cheap run is paid for."""
    return judge_name.endswith(':pre')


def is_graded(judge_name):
    """3-tier difficulty results, not a binary accept / escalate route."""
    return judge_name.endswith(':tier')


def judge(
    tasks,
    cache,
    source_arm,
    judges,
    env=None,
    workers=4,
    verifier_factory=None,
    kind='answer',
    log=print,
):
    """Judge ``source_arm``'s answers, or (task-only kinds) the tasks.

    Results are stored under ``source_arm`` so they line up with its runs.
    """
    env = dict(os.environ if env is None else env)
    store = RunCache(cache)
    runs = store.runs()
    have = store.judgments()
    by_id = {task.id: task for task in tasks}
    factory = verifier_factory or (lambda name: build_verifier(name, env))
    task_only = kind in TASK_ONLY_KINDS
    judged = []
    for judge_name in judges:
        name = judged_name(judge_name, kind)
        verifier = factory(judge_name)
        todo = [
            (task_id, run)
            for (task_id, arm), run in runs.items()
            if arm == source_arm
            and task_id in by_id
            and (task_id, arm, name) not in have
            and (task_only or run.get('result') is not None)
        ]
        what = 'tasks' if task_only else 'answers'
        log(f'judge {name}: {len(todo)} {what} to score')

        def judge_one(item, verifier=verifier, name=name):
            task_id, run = item
            task = by_id[task_id]
            candidate = None if task_only else run['result']
            verdict = verifier.verify(
                task.prompt, candidate, rubric=task.rubric, kind=kind
            )
            record = {
                'task_id': task_id,
                'arm': source_arm,
                'kind': kind,
                'ts': time.time(),
                **verdict.to_dict(),
                'judge': name,
            }
            store.add_judgment(record)
            return record

        try:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                judged += list(pool.map(judge_one, todo))
        finally:
            verifier.close()
    return judged


def regrade(tasks, cache, env=None, transport=None, log=print):
    """Re-apply each task's grader; append a record only on a change."""
    store = RunCache(cache)
    by_id = {task.id: task for task in tasks}
    changed = 0
    for (task_id, _arm), run in store.runs().items():
        task = by_id.get(task_id)
        if task is None or run.get('is_error'):
            continue
        correct, why = grade(
            task, run.get('result'), env=env, transport=transport
        )
        if (correct, why) == (run.get('correct'), run.get('grade')):
            continue
        store.add_run(
            {
                **run,
                'correct': correct,
                'grade': why,
                'regraded_from': run.get('correct'),
                'ts': time.time(),
            }
        )
        changed += 1
    log(f'regrade: {changed} runs changed')
    return changed


# --- hook logs --------------------------------------------------------------
def hooklog_summary(path):
    """Counts per hook kind from a davi-route hook log."""
    records = [
        record
        for record in read_jsonl(path)
        if record.get('event') == 'davi-route'
    ]
    summary = {'lines': len(records), 'by_kind': {}}
    for kind in ('pre', 'post', 'stop', 'subagent_stop'):
        kind_records = [r for r in records if r.get('kind') == kind]
        if kind_records:
            summary['by_kind'][kind] = _summarize_kind(kind, kind_records)
    return summary


def _summarize_kind(kind, records):
    out = {
        'n': len(records),
        'status': _count(r.get('status') for r in records),
        'decision': _count(
            r['decision'] for r in records if r.get('decision')
        ),
        'emitted': sum(1 for r in records if r.get('emitted')),
    }
    probabilities = sorted(
        r['p_accept']
        for r in records
        if isinstance(r.get('p_accept'), (int, float))
    )
    if probabilities:
        out['p_accept'] = {
            'n': len(probabilities),
            'min': probabilities[0],
            'median': probabilities[len(probabilities) // 2],
            'max': probabilities[-1],
        }
    costs = [
        r['cost_usd']
        for r in records
        if isinstance(r.get('cost_usd'), (int, float))
    ]
    if costs:
        out['judge_cost_total'] = sum(costs)
    if kind == 'pre':
        out['model_set'] = _count(
            f'{r.get("model_requested")} -> {r.get("model_set")} '
            f'({r.get("source")})'
            for r in records
            if r.get('changed')
        )
    return out


def _count(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts
