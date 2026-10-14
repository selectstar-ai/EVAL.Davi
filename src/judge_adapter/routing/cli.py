# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""``davi-route``: routing measurement CLI (see docs/routing.md).

  davi-route generate --tasks T.jsonl --arm cheap=haiku --arm strong=opus --cache runs/x
  davi-route judge    --tasks T.jsonl --cache runs/x --source-arm cheap --judge davi --judge jev --judge claude
  davi-route analyze  --cache runs/x --cheap cheap --strong strong --out runs/x/report
  davi-route verify   --judge jev --task "..." --candidate "..."
  davi-route hooklog  path/to/hooks.jsonl
  davi-route probe    --plugin-dir plugins/davi-route --out runs/probe

Every real step spends money (claude -p runs, judge calls). ``generate`` asks for --yes unless --dry-run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import harness as Hn
from . import probe as Pr
from . import verifiers as V


def _arms(a):
    arms = [Hn.parse_arm(s) for s in a.arm or []]
    if a.arms_file:
        arms += Hn.load_arms(a.arms_file)
    names = [x.name for x in arms]
    if not arms or len(set(names)) != len(names):
        raise SystemExit('give at least one arm (--arm name=model or --arms-file) with unique names')
    return arms


def cmd_generate(a):
    tasks = Hn.load_tasks(a.tasks)
    arms = _arms(a)
    if a.limit:
        tasks = tasks[:a.limit]
    n = len(tasks) * len(arms)
    print(f'{len(tasks)} tasks x {len(arms)} arms = up to {n} claude -p runs '
          f'({", ".join(f"{x.name}={x.model}" for x in arms)})')
    if a.dry_run:
        t = tasks[0]
        for arm in arms:
            print('  ' + ' '.join(Hn.claude_cmd(t, arm, a.claude_bin, a.max_turns, a.budget_usd)))
        return 0
    if not a.yes:
        raise SystemExit('this spends money; re-run with --yes (or --dry-run to see the commands)')
    Hn.generate(tasks, arms, a.cache, workers=a.workers, claude_bin=a.claude_bin, max_turns=a.max_turns,
                budget_usd=a.budget_usd, timeout=a.timeout, redo_errors=a.redo_errors)
    return 0


def cmd_judge(a):
    tasks = Hn.load_tasks(a.tasks)
    Hn.judge(tasks, a.cache, a.source_arm, a.judge, workers=a.workers, kind=a.kind)
    return 0


def cmd_analyze(a):
    out = a.out or os.path.join(a.cache, 'report')
    rep = Hn.analyze(a.cache, a.cheap, a.strong, calib_frac=a.calib_frac, seed=a.seed, tolerance=a.tolerance,
                     on_error=a.on_error, out_dir=out)
    print(Hn.report_markdown(rep))
    print(f'written: {out}/report.md, report.json, curve.csv')
    return 0


def cmd_verify(a):
    v = V.build_verifier(a.judge, dict(os.environ))
    try:
        r = v.verify(a.task, a.candidate, rubric=a.rubric, kind=a.kind)
    finally:
        v.close()
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    return 0 if r.outcome == 'ok' else 1


def cmd_hooklog(a):
    print(json.dumps(Hn.hooklog_summary(a.path), ensure_ascii=False, indent=2))
    return 0


def cmd_probe(a):
    rep = Pr.run_probes(a.plugin_dir, a.out, main_model=a.main_model, claude_bin=a.claude_bin,
                        which=tuple(a.run or ('agent', 'stop', 'pre')))
    print(Pr.checks_table(rep))
    print(f'details: {a.out}/probe-report.json, hook log: {a.out}/probe-hooks.jsonl')
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog='davi-route', description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)

    g = sub.add_parser('generate', help='run tasks with claude -p per arm (cached)')
    g.add_argument('--tasks', required=True)
    g.add_argument('--cache', required=True)
    g.add_argument('--arm', action='append', help='name=model[:extra claude args], repeatable')
    g.add_argument('--arms-file', help='JSON list of {name, model, args, env}')
    g.add_argument('--workers', type=int, default=2)
    g.add_argument('--max-turns', type=int)
    g.add_argument('--budget-usd', type=float, help='--max-budget-usd per run')
    g.add_argument('--timeout', type=int, default=900)
    g.add_argument('--limit', type=int)
    g.add_argument('--claude-bin', default='claude')
    g.add_argument('--redo-errors', action='store_true')
    g.add_argument('--dry-run', action='store_true')
    g.add_argument('--yes', action='store_true')
    g.set_defaults(fn=cmd_generate)

    j = sub.add_parser('judge', help='score one arm\'s answers with each judge (cached)')
    j.add_argument('--tasks', required=True)
    j.add_argument('--cache', required=True)
    j.add_argument('--source-arm', required=True)
    j.add_argument('--judge', action='append', required=True, choices=V.JUDGES)
    j.add_argument('--kind', default='answer', choices=sorted(V.KINDS))
    j.add_argument('--workers', type=int, default=4)
    j.set_defaults(fn=cmd_judge)

    n = sub.add_parser('analyze', help='curves, baselines and frozen-threshold results from the cache')
    n.add_argument('--cache', required=True)
    n.add_argument('--cheap', required=True)
    n.add_argument('--strong', required=True)
    n.add_argument('--out')
    n.add_argument('--calib-frac', type=float, default=0.3)
    n.add_argument('--seed', type=int, default=0)
    n.add_argument('--tolerance', type=float, default=0.02)
    n.add_argument('--on-error', default='accept', choices=('accept', 'escalate'))
    n.set_defaults(fn=cmd_analyze)

    v = sub.add_parser('verify', help='one verifier call (smoke test of a judge)')
    v.add_argument('--judge', required=True, choices=V.JUDGES)
    v.add_argument('--task', required=True)
    v.add_argument('--candidate', required=True)
    v.add_argument('--rubric')
    v.add_argument('--kind', default='answer', choices=sorted(V.KINDS))
    v.set_defaults(fn=cmd_verify)

    h = sub.add_parser('hooklog', help='summarize a davi-route hook log (A/B inside Claude Code)')
    h.add_argument('path')
    h.set_defaults(fn=cmd_hooklog)

    r = sub.add_parser('probe', help='check the hook mechanisms in a real headless Claude Code (checks 1-4)')
    r.add_argument('--plugin-dir', default='plugins/davi-route')
    r.add_argument('--out', default='runs/probe')
    r.add_argument('--main-model', default='sonnet')
    r.add_argument('--claude-bin', default='claude')
    r.add_argument('--run', action='append', choices=('agent', 'stop', 'pre'))
    r.set_defaults(fn=cmd_probe)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
