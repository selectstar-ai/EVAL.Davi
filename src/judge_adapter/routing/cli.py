# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""``davi-route``: routing measurement CLI (see docs/routing.md).

  davi-route generate --tasks T.jsonl --arm cheap=haiku --arm strong=opus \\
      --cache runs/x
  davi-route judge    --tasks T.jsonl --cache runs/x --source-arm cheap \\
      --judge davi --judge jev --judge claude [--kind pre|tier]
  davi-route analyze  --cache runs/x --cheap cheap --strong strong
  davi-route verify   --judge jev --task "..." --candidate "..."
  davi-route regrade  --tasks T.jsonl --cache runs/x
  davi-route curate   --tasks T.jsonl --cache runs/x --ours davi --n 20 \\
      --out runs/x/demo.tasks.jsonl
  davi-route demo     --cache runs/x [--tasks demo.tasks.jsonl]
  davi-route hooklog  path/to/hooks.jsonl
  davi-route probe    --plugin-dir plugins/davi-route --out runs/probe

Every real step spends money (claude -p runs, judge calls). ``generate``
asks for --yes unless --dry-run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import analysis, harness, probe
from .questions import KINDS
from .verifiers import JUDGES, build_verifier

ON_ERROR_CHOICES = ('accept', 'escalate')


def _arms(args):
    arms = [harness.parse_arm(spec) for spec in args.arm or []]
    if args.arms_file:
        arms += harness.load_arms(args.arms_file)
    names = [arm.name for arm in arms]
    if not arms or len(set(names)) != len(names):
        raise SystemExit(
            'give at least one arm (--arm name=model or --arms-file) with '
            'unique names'
        )
    return arms


def cmd_generate(args):
    tasks = harness.load_tasks(args.tasks)
    arms = _arms(args)
    if args.limit:
        tasks = tasks[: args.limit]
    described = ', '.join(f'{arm.name}={arm.model}' for arm in arms)
    print(
        f'{len(tasks)} tasks x {len(arms)} arms = up to '
        f'{len(tasks) * len(arms)} claude -p runs ({described})'
    )
    if args.dry_run:
        for arm in arms:
            command = harness.claude_cmd(
                tasks[0],
                arm,
                args.claude_bin,
                args.max_turns,
                args.budget_usd,
            )
            print('  ' + ' '.join(command))
        return 0
    if not args.yes:
        raise SystemExit(
            'this spends money; re-run with --yes (or --dry-run to see the '
            'commands)'
        )
    harness.generate(
        tasks,
        arms,
        args.cache,
        workers=args.workers,
        claude_bin=args.claude_bin,
        max_turns=args.max_turns,
        budget_usd=args.budget_usd,
        timeout=args.timeout,
        redo_errors=args.redo_errors,
    )
    return 0


def cmd_judge(args):
    tasks = harness.load_tasks(args.tasks)
    harness.judge(
        tasks,
        args.cache,
        args.source_arm,
        args.judge,
        workers=args.workers,
        kind=args.kind,
    )
    return 0


def cmd_analyze(args):
    out = args.out or os.path.join(args.cache, 'report')
    report = analysis.analyze(
        args.cache,
        args.cheap,
        args.strong,
        calib_frac=args.calib_frac,
        seed=args.seed,
        tolerance=args.tolerance,
        on_error=args.on_error,
        out_dir=out,
    )
    print(analysis.report_markdown(report))
    print(f'written: {out}/report.md, report.json, curve.csv')
    return 0


def cmd_verify(args):
    verifier = build_verifier(args.judge, dict(os.environ))
    try:
        verdict = verifier.verify(
            args.task, args.candidate, rubric=args.rubric, kind=args.kind
        )
    finally:
        verifier.close()
    print(json.dumps(verdict.to_dict(), ensure_ascii=False, indent=2))
    return 0 if verdict.outcome == 'ok' else 1


def cmd_regrade(args):
    harness.regrade(harness.load_tasks(args.tasks), args.cache)
    return 0


def cmd_curate(args):
    selection = analysis.curate(
        args.cache,
        args.cheap,
        args.strong,
        ours=args.ours,
        n=args.n,
        accept_frac=args.accept_frac,
        threshold=args.threshold,
        min_margin=args.min_margin,
        require=args.require,
        on_error=args.on_error,
    )
    keep = set(selection['selected'])
    tasks = [
        task
        for task in harness.read_jsonl(args.tasks)
        if task.get('id') in keep
    ]
    out_dir = os.path.dirname(os.path.abspath(args.out)) or '.'
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False) + '\n')
    criteria = os.path.splitext(args.out)[0] + '.selection.json'
    with open(criteria, 'w', encoding='utf-8') as handle:
        json.dump(selection, handle, ensure_ascii=False, indent=2)
    picked = selection['picked']
    print(
        f'selected {len(tasks)} tasks (accept {picked["accept"]}, escalate '
        f'{picked["escalate"]}; available {selection["available"]}) -> '
        f'{args.out}'
    )
    rows, judges = analysis.demo_rows(
        args.cache,
        args.cheap,
        args.strong,
        args.threshold,
        args.on_error,
        task_ids=selection['selected'],
    )
    print(analysis.demo_text(rows, judges, args.threshold))
    return 0


def cmd_demo(args):
    ids = None
    if args.tasks:
        ids = [task['id'] for task in harness.read_jsonl(args.tasks)]
    rows, judges = analysis.demo_rows(
        args.cache,
        args.cheap,
        args.strong,
        args.threshold,
        args.on_error,
        task_ids=ids,
    )
    print(analysis.demo_text(rows, judges, args.threshold))
    return 0


def cmd_hooklog(args):
    summary = harness.hooklog_summary(args.path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_probe(args):
    report = probe.run_probes(
        args.plugin_dir,
        args.out,
        main_model=args.main_model,
        claude_bin=args.claude_bin,
        which=tuple(args.run or ('agent', 'stop', 'pre')),
    )
    print(probe.checks_table(report))
    print(
        f'details: {args.out}/probe-report.json, hook log: '
        f'{args.out}/probe-hooks.jsonl'
    )
    return 0


def _add_generate(sub):
    parser = sub.add_parser(
        'generate', help='run tasks with claude -p per arm (cached)'
    )
    parser.add_argument('--tasks', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument(
        '--arm',
        action='append',
        help='name=model[:extra claude args], repeatable',
    )
    parser.add_argument(
        '--arms-file', help='JSON list of {name, model, args, env}'
    )
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--max-turns', type=int)
    parser.add_argument(
        '--budget-usd', type=float, help='--max-budget-usd per run'
    )
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--claude-bin', default='claude')
    parser.add_argument('--redo-errors', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.set_defaults(fn=cmd_generate)


def _add_judge(sub):
    parser = sub.add_parser(
        'judge', help="score one arm's answers with each judge (cached)"
    )
    parser.add_argument('--tasks', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--source-arm', required=True)
    parser.add_argument(
        '--judge', action='append', required=True, choices=JUDGES
    )
    parser.add_argument('--kind', default='answer', choices=sorted(KINDS))
    parser.add_argument('--workers', type=int, default=4)
    parser.set_defaults(fn=cmd_judge)


def _add_analyze(sub):
    parser = sub.add_parser(
        'analyze',
        help='curves, baselines and frozen-threshold results from the cache',
    )
    parser.add_argument('--cache', required=True)
    parser.add_argument('--cheap', required=True)
    parser.add_argument('--strong', required=True)
    parser.add_argument('--out')
    parser.add_argument('--calib-frac', type=float, default=0.3)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--tolerance', type=float, default=0.02)
    parser.add_argument(
        '--on-error', default='accept', choices=ON_ERROR_CHOICES
    )
    parser.set_defaults(fn=cmd_analyze)


def _add_verify(sub):
    parser = sub.add_parser(
        'verify', help='one verifier call (smoke test of a judge)'
    )
    parser.add_argument('--judge', required=True, choices=JUDGES)
    parser.add_argument('--task', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--rubric')
    parser.add_argument('--kind', default='answer', choices=sorted(KINDS))
    parser.set_defaults(fn=cmd_verify)


def _add_regrade(sub):
    parser = sub.add_parser(
        'regrade', help='re-apply the task graders to cached runs'
    )
    parser.add_argument('--tasks', required=True)
    parser.add_argument('--cache', required=True)
    parser.set_defaults(fn=cmd_regrade)


def _add_demo_args(parser):
    parser.add_argument('--cache', required=True)
    parser.add_argument('--cheap', default='cheap')
    parser.add_argument('--strong', default='strong')
    parser.add_argument('--threshold', type=float, default=0.5)
    parser.add_argument(
        '--on-error', default='accept', choices=ON_ERROR_CHOICES
    )


def _add_curate(sub):
    parser = sub.add_parser(
        'curate',
        help='pick a DEMO subset where one judge routes right and the '
        'others do not',
    )
    _add_demo_args(parser)
    parser.add_argument('--tasks', required=True)
    parser.add_argument(
        '--out',
        required=True,
        help='tasks JSONL of the selection (+ .selection.json with the '
        'criteria)',
    )
    parser.add_argument('--ours', default='davi')
    parser.add_argument('--n', type=int, default=20)
    parser.add_argument('--accept-frac', type=float, default=0.5)
    parser.add_argument(
        '--min-margin',
        type=float,
        default=0.0,
        help='|P(ours) - threshold| at least this',
    )
    parser.add_argument(
        '--require',
        default='all',
        choices=('all', 'any'),
        help='rivals that must route wrong',
    )
    parser.set_defaults(fn=cmd_curate)


def _add_demo(sub):
    parser = sub.add_parser(
        'demo',
        help='per task: P -> route -> correct -> cost for each judge, then '
        'a summary',
    )
    _add_demo_args(parser)
    parser.add_argument(
        '--tasks', help='limit to (and order by) the tasks in this JSONL'
    )
    parser.set_defaults(fn=cmd_demo)


def _add_hooklog(sub):
    parser = sub.add_parser('hooklog', help='summarize a davi-route hook log')
    parser.add_argument('path')
    parser.set_defaults(fn=cmd_hooklog)


def _add_probe(sub):
    parser = sub.add_parser(
        'probe',
        help='check the hook mechanisms in a real headless Claude Code',
    )
    parser.add_argument('--plugin-dir', default='plugins/davi-route')
    parser.add_argument('--out', default='runs/probe')
    parser.add_argument('--main-model', default='sonnet')
    parser.add_argument('--claude-bin', default='claude')
    parser.add_argument(
        '--run', action='append', choices=('agent', 'stop', 'pre')
    )
    parser.set_defaults(fn=cmd_probe)


SUBCOMMANDS = (
    _add_generate,
    _add_judge,
    _add_analyze,
    _add_verify,
    _add_regrade,
    _add_curate,
    _add_demo,
    _add_hooklog,
    _add_probe,
)


def build_parser():
    parser = argparse.ArgumentParser(
        prog='davi-route', description=__doc__.split('\n')[0]
    )
    sub = parser.add_subparsers(dest='cmd', required=True)
    for add in SUBCOMMANDS:
        add(sub)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
