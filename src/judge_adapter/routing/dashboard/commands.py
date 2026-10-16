# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""``davi-route dashboard`` and ``davi-route record``.

  davi-route dashboard --tasks demo.tasks.jsonl [--port 8765] [--no-live]
  davi-route record    --tasks demo.tasks.jsonl --yes [--id ID ...]

A live run starts two ``claude -p`` sessions (routed and always-opus) and
spends money; ``record`` runs many questions in a row for later replay.
"""

from __future__ import annotations

import os
import shutil
import webbrowser

from ..config import SECRET_ENVS, first_env
from ..policy import TierPolicy
from .runs import LiveRun
from .server import DashboardApp, DashboardServer, default_record_dir
from .sessions import SessionConfig
from .store import RecordingStore
from .tasks import load_demo_tasks

ALL_JUDGES = ('davi', 'jev', 'claude')
JUDGE_KEYS = {'jev': 'jev', 'claude': 'claude'}


def available_judges(env):
    """Davi always; Jev and Claude when their keys are set."""
    return tuple(
        name
        for name in ALL_JUDGES
        if name not in JUDGE_KEYS
        or first_env(env, SECRET_ENVS[JUDGE_KEYS[name]])
    )


def live_problem(env, claude_bin):
    """Why a live run cannot start, or None."""
    if shutil.which(claude_bin) is None:
        return f'{claude_bin} 명령을 찾을 수 없습니다.'
    if not (env.get('DAVI_BASE_URL') or env.get('DAVI_ROUTE_DAVI_BASE_URL')):
        return 'DAVI_BASE_URL이 설정되지 않았습니다.'
    return None


def session_config(args, env):
    return SessionConfig(
        plugin_dir=args.plugin_dir,
        claude_bin=args.claude_bin,
        main_model=args.main_model,
        timeout=args.timeout,
        budget_usd=args.budget_usd,
        env=env,
        grade_env=env,
    )


def _policy(args):
    return TierPolicy(low=args.tier_low, high=args.tier_high)


def cmd_dashboard(args):
    env = dict(os.environ)
    tasks = load_demo_tasks(args.tasks) if args.tasks else []
    store = RecordingStore(args.record_dir)
    problem = None if args.no_live else live_problem(env, args.claude_bin)
    config = None if args.no_live else session_config(args, env)
    app = DashboardApp(
        tasks,
        store,
        config=config,
        judges=tuple(args.judge or available_judges(env)),
        policy=_policy(args),
        live_problem=problem,
    )
    server = DashboardServer((args.host, args.port), app)
    print(f'dashboard: {server.url}')
    print(f'  questions {len(tasks)}, recordings in {store.directory}')
    if app.live_enabled:
        print(
            '  live runs ON: each run starts two claude -p sessions '
            '(routed + always opus) and spends money'
        )
    else:
        print(f'  live runs OFF: {app.state()["live"]["problem"]}')
    if args.open:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_record(args):
    env = dict(os.environ)
    tasks = load_demo_tasks(args.tasks)
    if args.id:
        wanted = set(args.id)
        tasks = [task for task in tasks if task.id in wanted]
    problem = live_problem(env, args.claude_bin)
    if problem:
        raise SystemExit(problem)
    print(
        f'{len(tasks)} questions x 2 sessions (routed + always opus) = '
        f'{2 * len(tasks)} claude -p runs'
    )
    if not args.yes:
        raise SystemExit('this spends money; re-run with --yes')
    store = RecordingStore(args.record_dir)
    config = session_config(args, env)
    judges = tuple(args.judge or available_judges(env))
    for number, task in enumerate(tasks, 1):
        run = LiveRun(
            f'record-{number}',
            task,
            config,
            store=store,
            judges=judges,
            policy=_policy(args),
        )
        run.run()
        print(_record_line(number, task, run.channel.events()))
    print(f'recordings: {store.directory}')
    return 0


def _record_line(number, task, events):
    summary, recording_id = {}, None
    for event in events:
        if event.type == 'summary':
            summary = event.data
        elif event.type == 'run_done':
            recording_id = event.data.get('recording_id')
    cost = summary.get('routed_cost_usd')
    opus = summary.get('opus_cost_usd')
    costs = (
        '-'
        if cost is None or opus is None
        else (f'${cost:.3f} vs opus ${opus:.3f}')
    )
    return (
        f'{number:>3} {task.id[-12:]:<12} -> {summary.get("routed_model")} '
        f'correct {summary.get("routed_correct")} / opus '
        f'{summary.get("opus_correct")} | {costs} | {recording_id}'
    )


def _add_common(parser):
    parser.add_argument('--record-dir', default=default_record_dir())
    parser.add_argument('--plugin-dir', default='plugins/davi-route')
    parser.add_argument('--claude-bin', default='claude')
    parser.add_argument('--main-model', default='sonnet')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--budget-usd', type=float, default=1.0)
    parser.add_argument('--tier-low', type=float, default=TierPolicy.low)
    parser.add_argument('--tier-high', type=float, default=TierPolicy.high)
    parser.add_argument(
        '--judge',
        action='append',
        choices=ALL_JUDGES,
        help='judges to compare (default: those with keys)',
    )


def add_dashboard(sub):
    parser = sub.add_parser(
        'dashboard', help='local web dashboard that shows routing live'
    )
    parser.add_argument('--tasks', help='demo questions (JSONL)')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument(
        '--no-live', action='store_true', help='replay recordings only'
    )
    parser.add_argument(
        '--open', action='store_true', help='open the page in a browser'
    )
    _add_common(parser)
    parser.set_defaults(fn=cmd_dashboard)


def add_record(sub):
    parser = sub.add_parser(
        'record', help='run demo questions live and save them for replay'
    )
    parser.add_argument('--tasks', required=True)
    parser.add_argument('--id', action='append', help='only these task ids')
    parser.add_argument('--yes', action='store_true')
    _add_common(parser)
    parser.set_defaults(fn=cmd_record)
