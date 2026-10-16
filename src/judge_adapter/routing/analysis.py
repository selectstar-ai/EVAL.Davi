# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Offline analysis of a run cache (see ``harness``).

``analyze``  always-cheap / always-strong / oracle / random-at-matched-rate
             baselines, a threshold sweep per judge (accuracy vs cost incl.
             judge cost), AUROC of P(accept) for "the cheap answer is
             correct", and a FROZEN threshold chosen on a calibration split
             and reported on the held-out split. Also a per-arm summary.

Demo helpers (fixed threshold, no split; they show the flow, not claims):

``curate``   pick tasks where one judge ("ours") routes right and the others
             do not. A selection on outcomes: its numbers describe the
             selected tasks only.
``demo``     per task, each judge's P(accept) -> route -> final answer
             correct -> cost, then a summary next to always-cheap and
             always-strong.

An escalation in the replay = cheap run + judge + a fresh strong run (the
strong answer replaces the cheap one), which is what the live loop does,
without paying for it once per threshold. An up-front (``pre``) judgment
skips the cheap run of an escalated task.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass, replace

from .harness import RunCache, is_graded, is_pre

ON_ERROR = ('accept', 'escalate')
CURVE_COLUMNS = (
    'judge',
    'threshold',
    'escalation_rate',
    'accuracy',
    'cost_per_task',
    'random_accuracy',
    'random_cost_per_task',
)
DEMO_NOTE = (
    'selected on outcomes for a demo; not an estimate of general performance'
)


def auroc(scores, labels):
    """P(score of a positive > score of a negative), ties count half.

    None when a class is empty.
    """
    positives = [s for s, label in zip(scores, labels) if label]
    negatives = [s for s, label in zip(scores, labels) if not label]
    if not positives or not negatives:
        return None
    pairs = sorted([(s, 1) for s in positives] + [(s, 0) for s in negatives])
    rank_sum, start = 0.0, 0
    while start < len(pairs):
        end = start
        while end < len(pairs) and pairs[end][0] == pairs[start][0]:
            end += 1
        average_rank = (start + end + 1) / 2.0
        hits = sum(1 for k in range(start, end) if pairs[k][1] == 1)
        rank_sum += average_rank * hits
        start = end
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None


def split_ids(ids, calib_frac, seed):
    """Deterministic calibration / test split by hashing (seed, id)."""
    calib, test = [], []
    for task_id in ids:
        digest = hashlib.sha256(f'{seed}|{task_id}'.encode()).hexdigest()
        share = int(digest[:8], 16) / 0xFFFFFFFF
        (calib if share < calib_frac else test).append(task_id)
    return calib, test


def route_of(p, threshold, on_error='accept'):
    """'escalate' when P(accept) is below ``threshold``; None ->
    ``on_error``."""
    if p is None:
        return 'escalate' if on_error == 'escalate' else 'accept'
    return 'escalate' if p < threshold else 'accept'


def ideal_route(cheap_ok, strong_ok):
    """The route that is right in hindsight.

    Keep a correct cheap answer, escalate a wrong one the strong model
    fixes; None when both are wrong (no route changes the outcome).
    """
    if cheap_ok:
        return 'accept'
    return 'escalate' if strong_ok else None


# --- replay -----------------------------------------------------------------
@dataclass(frozen=True)
class ReplayRow:
    """One task with graded cheap and strong runs, plus one judgment."""

    id: str
    cheap_ok: bool
    strong_ok: bool
    cheap_cost: float
    strong_cost: float
    p: float | None = None
    judge_cost: float | None = None
    latency_s: float | None = None
    pre: bool = False

    def escalates(self, threshold, on_error='accept'):
        return route_of(self.p, threshold, on_error) == 'escalate'

    def correct(self, escalate):
        return self.strong_ok if escalate else self.cheap_ok

    def cost(self, escalate):
        """Cheap run (skipped by an escalating pre judgment) + judge +
        strong run when escalated."""
        first = 0.0 if (escalate and self.pre) else self.cheap_cost
        strong = self.strong_cost if escalate else 0.0
        return first + (self.judge_cost or 0.0) + strong


def replay_point(rows, threshold, on_error='accept'):
    """Accuracy, cost and escalation rate of one threshold over ``rows``."""
    correct = cost = escalated = 0.0
    for row in rows:
        escalate = row.escalates(threshold, on_error)
        escalated += escalate
        correct += row.correct(escalate)
        cost += row.cost(escalate)
    n = len(rows)
    return {
        'threshold': threshold,
        'escalation_rate': escalated / n,
        'accuracy': correct / n,
        'cost_per_task': cost / n,
    }


def random_point(rows, rate):
    """Escalating a random ``rate`` of the tasks, in expectation."""
    n = len(rows)
    accuracy = (
        sum((1 - rate) * row.cheap_ok + rate * row.strong_ok for row in rows)
        / n
    )
    cost = sum(row.cheap_cost + rate * row.strong_cost for row in rows) / n
    return {
        'escalation_rate': rate,
        'accuracy': accuracy,
        'cost_per_task': cost,
    }


def baselines(rows):
    n = len(rows)
    return {
        'always_cheap': {
            'accuracy': sum(row.cheap_ok for row in rows) / n,
            'cost_per_task': sum(row.cheap_cost for row in rows) / n,
            'escalation_rate': 0.0,
        },
        'always_strong': {
            'accuracy': sum(row.strong_ok for row in rows) / n,
            'cost_per_task': sum(row.strong_cost for row in rows) / n,
            'escalation_rate': 1.0,
        },
        'oracle': {
            'accuracy': sum(max(row.cheap_ok, row.strong_ok) for row in rows)
            / n,
            'cost_per_task': sum(
                row.cheap_cost + (0 if row.cheap_ok else row.strong_cost)
                for row in rows
            )
            / n,
            'escalation_rate': sum(1 for row in rows if not row.cheap_ok) / n,
        },
    }


def sweep(rows, on_error='accept'):
    """One point per distinct P: each threshold escalates every P strictly
    below it."""
    probabilities = sorted({row.p for row in rows if row.p is not None})
    thresholds = [0.0] + [p + 1e-12 for p in probabilities]
    return [replay_point(rows, t, on_error) for t in thresholds]


def choose_threshold(rows, tolerance, on_error='accept'):
    """The cheapest threshold whose accuracy is within ``tolerance`` of
    always-strong; else the most accurate one (ties -> cheaper)."""
    strong = baselines(rows)['always_strong']['accuracy']
    points = sweep(rows, on_error)
    good = [
        point
        for point in points
        if point['accuracy'] >= strong - tolerance - 1e-12
    ]
    if good:
        best = min(good, key=lambda pt: (pt['cost_per_task'], -pt['accuracy']))
        return best['threshold'], 'within_tolerance'
    best = max(points, key=lambda pt: (pt['accuracy'], -pt['cost_per_task']))
    return best['threshold'], 'max_accuracy'


# --- analyze ----------------------------------------------------------------
def arm_summary(runs):
    """Accuracy and cost per arm, from {(task_id, arm): run}."""
    by_arm = {}
    for run in runs.values():
        by_arm.setdefault(run['arm'], []).append(run)
    summary = {}
    for arm, arm_runs in sorted(by_arm.items()):
        graded = [r for r in arm_runs if r.get('correct') is not None]
        costs = [
            r['total_cost_usd']
            for r in arm_runs
            if isinstance(r.get('total_cost_usd'), (int, float))
        ]
        successes = sum(1 for r in graded if r['correct'])
        summary[arm] = {
            'n': len(arm_runs),
            'graded': len(graded),
            'accuracy': successes / len(graded) if graded else None,
            'cost_per_task': mean(costs),
            'total_cost': sum(costs),
            'cost_per_success': sum(costs) / successes if successes else None,
            'mean_wall_s': mean(
                r['wall_s']
                for r in arm_runs
                if isinstance(r.get('wall_s'), (int, float))
            ),
            'errors': sum(1 for r in arm_runs if r.get('is_error')),
            'subagents_spawned': sum(
                (r.get('subagents') or {}).get('spawned') or 0
                for r in arm_runs
            ),
            'models_seen': sorted(
                {
                    model
                    for r in arm_runs
                    for model in r.get('model_usage') or {}
                }
            ),
        }
    return summary


def paired_rows(runs, cheap, strong):
    """{task_id: ReplayRow} for tasks graded with known cost in both arms."""
    rows = {}
    for (task_id, arm), run in runs.items():
        if arm != cheap:
            continue
        strong_run = runs.get((task_id, strong))
        if not strong_run or not _graded_with_cost(run):
            continue
        if not _graded_with_cost(strong_run):
            continue
        rows[task_id] = ReplayRow(
            id=task_id,
            cheap_ok=bool(run['correct']),
            strong_ok=bool(strong_run['correct']),
            cheap_cost=run['total_cost_usd'],
            strong_cost=strong_run['total_cost_usd'],
        )
    return rows


def _graded_with_cost(run):
    return run.get('correct') is not None and isinstance(
        run.get('total_cost_usd'), (int, float)
    )


def analyze(
    cache,
    cheap,
    strong,
    calib_frac=0.3,
    seed=0,
    tolerance=0.02,
    on_error='accept',
    out_dir=None,
):
    store = RunCache(cache)
    runs = store.runs()
    judgments = store.judgments()
    report = {
        'arms': arm_summary(runs),
        'cheap_arm': cheap,
        'strong_arm': strong,
        'tolerance': tolerance,
        'calib_frac': calib_frac,
        'seed': seed,
        'judges': {},
        'notes': [],
    }
    base = paired_rows(runs, cheap, strong)
    report['n_paired'] = len(base)
    if not base:
        report['notes'].append(
            'no task has graded runs with known cost in both the cheap and '
            'the strong arm'
        )
        write_report(report, [], out_dir)
        return report
    report['baselines'] = baselines(list(base.values()))
    calib_ids, test_ids = split_ids(sorted(base), calib_frac, seed)
    judge_names = sorted(
        {
            name
            for (_, arm, name) in judgments
            if arm == cheap and not is_graded(name)
        }
    )
    curve = []
    for name in judge_names:
        rows = [
            replace(
                row,
                p=found.get('p_accept'),
                judge_cost=found.get('cost_usd'),
                latency_s=found.get('latency_s'),
                pre=is_pre(name),
            )
            for task_id, row in base.items()
            if (found := judgments.get((task_id, cheap, name))) is not None
        ]
        if not rows:
            continue
        curve += _curve_rows(name, rows, on_error)
        result = _judge_summary(rows)
        result['frozen'] = _frozen(
            rows, set(calib_ids), set(test_ids), tolerance, on_error
        )
        report['judges'][name] = result
    if len(base) < 100:
        report['notes'].append(
            f'only {len(base)} paired tasks: differences of a few points '
            'are within noise'
        )
    write_report(report, curve, out_dir)
    return report


def _curve_rows(name, rows, on_error):
    out = []
    for point in sweep(rows, on_error):
        rand = random_point(rows, point['escalation_rate'])
        out.append(
            {
                'judge': name,
                **point,
                'random_accuracy': rand['accuracy'],
                'random_cost_per_task': rand['cost_per_task'],
            }
        )
    return out


def _judge_summary(rows):
    scored = [row for row in rows if row.p is not None]
    return {
        'n': len(rows),
        'verifier_failures': sum(1 for row in rows if row.p is None),
        'unknown_judge_cost': sum(
            1 for row in rows if row.judge_cost is None and row.p is not None
        ),
        'auroc_cheap_correct': auroc(
            [row.p for row in scored], [row.cheap_ok for row in scored]
        ),
        'mean_judge_cost': mean(
            row.judge_cost for row in rows if row.judge_cost is not None
        ),
        'mean_judge_latency_s': mean(
            row.latency_s
            for row in rows
            if isinstance(row.latency_s, (int, float))
        ),
        'baselines': baselines(rows),
    }


def _frozen(rows, calib_ids, test_ids, tolerance, on_error):
    """Threshold chosen on the calibration rows, reported on the test
    rows."""
    calib = [row for row in rows if row.id in calib_ids]
    test = [row for row in rows if row.id in test_ids]
    if not calib or not test:
        return None
    threshold, chosen_by = choose_threshold(calib, tolerance, on_error)
    point = replay_point(test, threshold, on_error)
    base = baselines(test)
    rand = random_point(test, point['escalation_rate'])
    strong = base['always_strong']
    cheap_accuracy = base['always_cheap']['accuracy']
    gap = strong['accuracy'] - cheap_accuracy
    cost_vs_strong = None
    if strong['cost_per_task']:
        cost_vs_strong = point['cost_per_task'] / strong['cost_per_task']
    return {
        'threshold': threshold,
        'chosen_by': chosen_by,
        'n_calib': len(calib),
        'n_test': len(test),
        'test': point,
        'test_baselines': base,
        'test_random_same_rate': rand,
        'accuracy_minus_strong': point['accuracy'] - strong['accuracy'],
        'accuracy_minus_random': point['accuracy'] - rand['accuracy'],
        'cost_vs_strong': cost_vs_strong,
        'gap_closed': (
            (point['accuracy'] - cheap_accuracy) / gap if gap > 0 else None
        ),
    }


def fmt(value, pct=False, money=False):
    if value is None:
        return '-'
    if pct:
        return f'{100 * value:.1f}%'
    if money:
        return f'${value:.5f}'
    return f'{value:.3f}' if isinstance(value, float) else str(value)


def report_markdown(report):
    lines = [
        '# Routing report',
        '',
        f'cheap arm `{report["cheap_arm"]}`, strong arm '
        f'`{report["strong_arm"]}`, paired tasks '
        f'{report.get("n_paired", 0)}',
        '',
        '## Arms (end-to-end)',
        '',
        '| arm | n | accuracy | cost/task | cost/success | wall s '
        '| subagents | models |',
        '|---|---|---|---|---|---|---|---|',
    ]
    for arm, s in report['arms'].items():
        lines.append(
            f'| {arm} | {s["n"]} | {fmt(s["accuracy"], pct=True)} | '
            f'{fmt(s["cost_per_task"], money=True)} | '
            f'{fmt(s["cost_per_success"], money=True)} | '
            f'{fmt(s["mean_wall_s"])} | {s["subagents_spawned"]} | '
            f'{", ".join(s["models_seen"])} |'
        )
    if report.get('baselines'):
        lines += [
            '',
            '## Baselines (all paired tasks)',
            '',
            '| policy | accuracy | cost/task | escalation |',
            '|---|---|---|---|',
        ]
        for name, b in report['baselines'].items():
            lines.append(
                f'| {name} | {fmt(b["accuracy"], pct=True)} | '
                f'{fmt(b["cost_per_task"], money=True)} | '
                f'{fmt(b["escalation_rate"], pct=True)} |'
            )
    if report['judges']:
        lines += _judge_table(report)
    if report['notes']:
        lines += ['', '## Notes', '']
        lines += [f'- {note}' for note in report['notes']]
    lines += [
        '',
        'Escalated tasks are charged cheap run + judge + strong run. Costs '
        'are `total_cost_usd` from Claude Code (list-price estimates) and '
        'judge estimates; not bills.',
    ]
    return '\n'.join(lines) + '\n'


def _judge_table(report):
    lines = [
        '',
        f'## Judges: frozen threshold (calibration '
        f'{report["calib_frac"]:.0%}, tolerance {report["tolerance"]:.0%} '
        'vs always-strong), held-out split',
        '',
        '| judge | AUROC | thr | test acc | strong acc | random acc (same '
        'rate) | escalation | cost/task | cost vs strong | gap closed | '
        'judge $/call | judge s |',
        '|---|---|---|---|---|---|---|---|---|---|---|---|',
    ]
    for name, result in report['judges'].items():
        frozen = result.get('frozen') or {}
        test = frozen.get('test') or {}
        strong = (frozen.get('test_baselines') or {}).get('always_strong')
        strong = strong or {}
        rand = frozen.get('test_random_same_rate') or {}
        lines.append(
            f'| {name} | {fmt(result["auroc_cheap_correct"])} | '
            f'{fmt(frozen.get("threshold"))} | '
            f'{fmt(test.get("accuracy"), pct=True)} | '
            f'{fmt(strong.get("accuracy"), pct=True)} | '
            f'{fmt(rand.get("accuracy"), pct=True)} | '
            f'{fmt(test.get("escalation_rate"), pct=True)} | '
            f'{fmt(test.get("cost_per_task"), money=True)} | '
            f'{fmt(frozen.get("cost_vs_strong"), pct=True)} | '
            f'{fmt(frozen.get("gap_closed"), pct=True)} | '
            f'{fmt(result["mean_judge_cost"], money=True)} | '
            f'{fmt(result["mean_judge_latency_s"])} |'
        )
    failures = ', '.join(
        f'{name} {r["verifier_failures"]}/{r["n"]}'
        for name, r in report['judges'].items()
    )
    return lines + ['', 'Failures: ' + failures]


def write_report(report, curve_rows, out_dir):
    """report.json, report.md and curve.csv in ``out_dir`` (if given)."""
    if not out_dir:
        return
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, 'report.json')
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    path = os.path.join(out_dir, 'report.md')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(report_markdown(report))
    path = os.path.join(out_dir, 'curve.csv')
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CURVE_COLUMNS))
        writer.writeheader()
        for row in curve_rows:
            writer.writerow({key: row.get(key) for key in CURVE_COLUMNS})


# --- demo -------------------------------------------------------------------
def last_line(text, width=60):
    """The last non-empty line of ``text``, cut to ``width``."""
    lines = [
        line.strip()
        for line in str(text or '').strip().splitlines()
        if line.strip()
    ]
    line = lines[-1] if lines else ''
    return line if len(line) <= width else line[: width - 1] + '…'


def demo_rows(
    cache,
    cheap,
    strong,
    threshold=0.5,
    on_error='accept',
    task_ids=None,
):
    """One row per task with graded cheap and strong runs: each judge's
    P -> route -> final correct -> cost. A missing judgment is None."""
    store = RunCache(cache)
    runs = store.runs()
    judgments = store.judgments()
    judges = sorted(
        {
            name
            for (_, arm, name) in judgments
            if arm == cheap and not is_graded(name)
        }
    )
    if task_ids is None:
        ids = sorted({task_id for (task_id, arm) in runs if arm == cheap})
    else:
        ids = list(task_ids)
    rows = []
    for task_id in ids:
        cheap_run = runs.get((task_id, cheap))
        strong_run = runs.get((task_id, strong))
        if not cheap_run or not strong_run:
            continue
        if cheap_run.get('correct') is None:
            continue
        if strong_run.get('correct') is None:
            continue
        row = _demo_row(task_id, cheap_run, strong_run)
        for name in judges:
            found = judgments.get((task_id, cheap, name))
            row['judges'][name] = _demo_cell(
                row, name, found, threshold, on_error
            )
        rows.append(row)
    return rows, judges


def _demo_row(task_id, cheap_run, strong_run):
    return {
        'id': task_id,
        'cheap_ok': bool(cheap_run['correct']),
        'strong_ok': bool(strong_run['correct']),
        'cheap_cost': cheap_run.get('total_cost_usd') or 0.0,
        'strong_cost': strong_run.get('total_cost_usd') or 0.0,
        'cheap_answer': last_line(cheap_run.get('result')),
        'strong_answer': last_line(strong_run.get('result')),
        'ideal': ideal_route(cheap_run['correct'], strong_run['correct']),
        'judges': {},
    }


def _demo_cell(row, name, judgment, threshold, on_error):
    if judgment is None:
        return None
    replay = ReplayRow(
        id=row['id'],
        cheap_ok=row['cheap_ok'],
        strong_ok=row['strong_ok'],
        cheap_cost=row['cheap_cost'],
        strong_cost=row['strong_cost'],
        p=judgment.get('p_accept'),
        judge_cost=judgment.get('cost_usd'),
        pre=is_pre(name),
    )
    route = route_of(replay.p, threshold, on_error)
    escalate = route == 'escalate'
    return {
        'p': replay.p,
        'route': route,
        'correct': replay.correct(escalate),
        'judge_cost': replay.judge_cost,
        'cost': replay.cost(escalate),
        'right_route': row['ideal'] is None or route == row['ideal'],
    }


def demo_summary(rows, judges):
    """Accuracy, strong-model rate and total cost per policy."""
    n = len(rows)
    if not n:
        return {}
    summary = {
        'always_cheap': {
            'accuracy': sum(row['cheap_ok'] for row in rows) / n,
            'strong_rate': 0.0,
            'total_cost': sum(row['cheap_cost'] for row in rows),
            'judge_cost': 0.0,
            'n': n,
        },
        'always_strong': {
            'accuracy': sum(row['strong_ok'] for row in rows) / n,
            'strong_rate': 1.0,
            'total_cost': sum(row['strong_cost'] for row in rows),
            'judge_cost': 0.0,
            'n': n,
        },
    }
    for name in judges:
        cells = [
            row['judges'][name] for row in rows if row['judges'].get(name)
        ]
        if not cells:
            continue
        m = len(cells)
        summary[name] = {
            'accuracy': sum(cell['correct'] for cell in cells) / m,
            'strong_rate': sum(cell['route'] == 'escalate' for cell in cells)
            / m,
            'total_cost': sum(cell['cost'] for cell in cells),
            'judge_cost': sum(cell['judge_cost'] or 0.0 for cell in cells),
            'unknown_judge_cost': sum(
                cell['judge_cost'] is None for cell in cells
            ),
            'right_route': sum(cell['right_route'] for cell in cells) / m,
            'n': m,
        }
    return summary


def curate(
    cache,
    cheap,
    strong,
    ours='davi',
    n=20,
    accept_frac=0.5,
    threshold=0.5,
    min_margin=0.0,
    require='all',
    on_error='accept',
):
    """Up to ``n`` tasks where ``ours`` takes the right route and the other
    judges do not.

    ``require='all'``: every other judge is wrong; ``'any'``: at least one.
    ``accept_frac`` of them are tasks whose right route is accept, the rest
    escalate tasks; a short side is filled from the other. Within a side,
    tasks with more wrong rivals and ``ours`` farther from the threshold
    come first. Tasks where both models are wrong are skipped.
    """
    rows, judges = demo_rows(cache, cheap, strong, threshold, on_error)
    if ours not in judges:
        raise ValueError(
            f'no judgments by {ours!r} in {cache} (have {judges})'
        )
    rivals = [name for name in judges if name != ours]
    needed = len(rivals) if require == 'all' else 1
    pools = {'accept': [], 'escalate': []}
    for row in rows:
        mine = row['judges'].get(ours)
        if row['ideal'] is None or not mine:
            continue
        if not all(row['judges'].get(name) for name in rivals):
            continue
        if mine['route'] != row['ideal'] or mine['p'] is None:
            continue
        margin = abs(mine['p'] - threshold)
        if margin < min_margin:
            continue
        wrong = sum(
            row['judges'][name]['route'] != row['ideal'] for name in rivals
        )
        if wrong < needed or (rivals and wrong == 0):
            continue
        pools[row['ideal']].append((-wrong, -margin, row['id']))
    for pool in pools.values():
        pool.sort()
    k_accept = min(len(pools['accept']), round(n * accept_frac))
    k_escalate = min(len(pools['escalate']), n - k_accept)
    k_accept = min(len(pools['accept']), n - k_escalate)
    picked = [item[2] for item in pools['accept'][:k_accept]]
    picked += [item[2] for item in pools['escalate'][:k_escalate]]
    return {
        'selected': picked,
        'ours': ours,
        'rivals': rivals,
        'threshold': threshold,
        'require': require,
        'min_margin': min_margin,
        'available': {key: len(pool) for key, pool in pools.items()},
        'picked': {'accept': k_accept, 'escalate': k_escalate},
        'n_pool': len(rows),
        'note': DEMO_NOTE,
    }


def demo_text(rows, judges, threshold, width=36):
    """A plain-text table of ``demo_rows`` plus the summary."""
    lines = [
        f'threshold {threshold}: escalate when P(accept) < threshold. '
        'O = final answer correct, X = wrong.',
        '',
    ]
    head = f'{"task":<{width}} {"haiku":<5} {"opus":<5} ' + ' '.join(
        f'{name:<22}' for name in judges
    )
    lines += [head, '-' * len(head)]
    for row in rows:
        cells = [_demo_text_cell(row['judges'].get(name)) for name in judges]
        lines.append(
            f'{row["id"][:width]:<{width}} {_mark(row["cheap_ok"]):<5} '
            f'{_mark(row["strong_ok"]):<5} ' + ' '.join(cells)
        )
    summary = demo_summary(rows, judges)
    if summary:
        lines += _summary_text(summary)
    lines += [
        '',
        'Costs: Claude Code total_cost_usd and judge estimates (list '
        'prices, not bills). Escalated = cheap + judge + strong.',
    ]
    return '\n'.join(lines) + '\n'


def _mark(ok):
    return 'O' if ok else 'X'


def _demo_text_cell(cell):
    if not cell:
        return f'{"-":<22}'
    p = '  -  ' if cell['p'] is None else f'{cell["p"]:.2f}'
    text = (
        f'{p} {cell["route"]:<8} {_mark(cell["correct"])} ${cell["cost"]:.3f}'
    )
    return text.ljust(22)


def _summary_text(summary):
    lines = [
        '',
        f'{"policy":<14} {"n":>3} {"accuracy":>9} {"strong calls":>13} '
        f'{"total cost":>11} {"judge cost":>11} {"right route":>12}',
        '-' * 80,
    ]
    for name, value in summary.items():
        right = '-'
        if 'right_route' in value:
            right = f'{100 * value["right_route"]:.0f}%'
        judge_cost = f'${value["judge_cost"]:.4f}'
        if value.get('unknown_judge_cost'):
            judge_cost += '*'
        lines.append(
            f'{name:<14} {value["n"]:>3} {100 * value["accuracy"]:>8.0f}% '
            f'{100 * value["strong_rate"]:>12.0f}% '
            f'${value["total_cost"]:>10.4f} {judge_cost:>11} {right:>12}'
        )
    if any(value.get('unknown_judge_cost') for value in summary.values()):
        lines.append('* some judge costs unknown (counted as 0)')
    return lines
