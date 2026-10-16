# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""One headless Claude Code session (``claude -p``) with the davi-route
plugin, watched through its hook log while it runs."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field

from ..grading import grade
from ..harness import Arm, claude_cmd, run_claude
from .events import ROUTING, SESSION_DONE, SESSION_STARTED, SUBAGENT
from .tasks import TIER_LABELS

# what a nested session may inherit; never ANTHROPIC_API_KEY (it would
# bill the API key instead of the logged-in plan)
PASSTHROUGH_ENVS = (
    'HOME',
    'USER',
    'LOGNAME',
    'PATH',
    'LANG',
    'LC_ALL',
    'TMPDIR',
    'SHELL',
)
DAVI_ENVS = ('DAVI_BASE_URL', 'DAVI_API_KEY')
EXTRA_ARGS = (
    '--setting-sources',
    'project,local',
    '--strict-mcp-config',
    '--no-session-persistence',
    '--disallowedTools',
    'WebSearch',
    'WebFetch',
)
MAX_RESULT_CHARS = 4000
SUBAGENT_FIELDS = (
    'ts',
    'resolved_model',
    'subagent_ms',
    'subagent_tokens',
    'subagent_output_tokens',
    'subagent_tool_uses',
    'subagent_status',
)
POLL_SECONDS = 0.25


def answer_line(text):
    """The "정답: ..." line of an answer, else its last line."""
    lines = [line.strip() for line in str(text or '').splitlines()]
    lines = [line for line in lines if line]
    for line in reversed(lines):
        if '정답' in line:
            return line
    return lines[-1] if lines else ''


class HookLogTail:
    """New JSON records of a growing JSONL file (partial lines wait)."""

    def __init__(self, path):
        self._path = path
        self._offset = 0
        self._partial = b''

    def read_new(self):
        try:
            with open(self._path, 'rb') as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except FileNotFoundError:
            return []
        self._offset += len(chunk)
        lines = (self._partial + chunk).split(b'\n')
        self._partial = lines.pop()
        records = []
        for line in lines:
            try:
                record = json.loads(line.decode('utf-8'))
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records


@dataclass(frozen=True)
class SessionConfig:
    """How sessions are started (shared by both arms)."""

    plugin_dir: str
    claude_bin: str = 'claude'
    main_model: str = 'sonnet'
    timeout: int = 900
    budget_usd: float = 1.0
    env: dict = field(default_factory=dict)  # source of passthrough envs
    grade_env: dict = field(default_factory=dict)
    work_root: str | None = None


class ClaudeSession:
    """Runs one arm on one task and reports it to an event channel."""

    def __init__(self, arm, config, runner=run_claude):
        self._arm = arm
        self._config = config
        self._runner = runner

    @property
    def arm(self):
        return self._arm

    def child_env(self, log_file, state_dir):
        """The environment of the nested session."""
        source = self._config.env
        names = PASSTHROUGH_ENVS
        if self._arm.needs_davi:
            names += DAVI_ENVS
        env = {name: source[name] for name in names if source.get(name)}
        env.update(
            {
                f'DAVI_ROUTE_{key}': value
                for key, value in self._arm.options.items()
            }
        )
        env['DAVI_ROUTE_LOG_FILE'] = log_file
        env['DAVI_ROUTE_STATE_DIR'] = state_dir
        return env

    def command(self, task):
        config = self._config
        args = ['--plugin-dir', os.path.abspath(config.plugin_dir)]
        arm = Arm(self._arm.key, config.main_model, args + list(EXTRA_ARGS))
        return claude_cmd(
            task.to_task(),
            arm,
            config.claude_bin,
            budget_usd=config.budget_usd,
        )

    def run(self, task, channel):
        """Run to the end; returns the ``session_done`` data."""
        work = tempfile.mkdtemp(
            prefix=f'davi-dashboard-{self._arm.key}-',
            dir=self._config.work_root,
        )
        cwd = os.path.join(work, 'cwd')
        os.makedirs(cwd)
        log_file = os.path.join(work, 'hooks.jsonl')
        env = self.child_env(log_file, os.path.join(work, 'state'))
        channel.emit(
            SESSION_STARTED,
            {
                'arm': self._arm.key,
                'label': self._arm.label,
                'main_model': self._config.main_model,
                'started_at': time.time(),
            },
        )
        box = {}
        worker = threading.Thread(
            target=self._call, args=(task, env, cwd, box), daemon=True
        )
        worker.start()
        tail = HookLogTail(log_file)
        while worker.is_alive():
            self._forward(tail.read_new(), channel)
            worker.join(POLL_SECONDS)
        self._forward(tail.read_new(), channel)
        done = self._summary(task, box)
        channel.emit(SESSION_DONE, done)
        return done

    def _call(self, task, env, cwd, box):
        try:
            box['result'] = self._runner(
                self.command(task), env, cwd, self._config.timeout
            )
        except Exception as error:  # noqa: BLE001 - reported as an event
            box['error'] = f'{type(error).__name__}: {error}'[:300]

    def _forward(self, records, channel):
        for record in records:
            if record.get('event') != 'davi-route':
                continue
            kind = record.get('kind')
            if kind == 'pre':
                channel.emit(ROUTING, self._routing(record))
            elif kind == 'post':
                channel.emit(
                    SUBAGENT,
                    {
                        'arm': self._arm.key,
                        'model_in': record.get('model_in'),
                        **{key: record.get(key) for key in SUBAGENT_FIELDS},
                    },
                )

    def _routing(self, record):
        tier = record.get('tier')
        return {
            'arm': self._arm.key,
            'status': record.get('status'),
            'source': record.get('source'),
            'model_requested': record.get('model_requested'),
            'model_set': record.get('model_set'),
            'level': record.get('level'),
            'level_probabilities': record.get('level_probabilities'),
            'tier': tier,
            'tier_label': TIER_LABELS.get(tier),
            'tier_low': record.get('tier_low'),
            'tier_high': record.get('tier_high'),
            'judge': record.get('judge'),
            'outcome': record.get('outcome'),
            'latency_s': record.get('latency_s'),
            'detail': record.get('detail'),
            # hook start (epoch) and the whole hook's time, judge included
            'ts': record.get('ts'),
            'seconds': record.get('seconds'),
        }

    def _summary(self, task, box):
        result = box.get('result') or {}
        text = result.get('result')
        error = box.get('error')
        failed = bool(error) or bool(result.get('is_error')) or not result
        if failed:
            correct, why = None, 'run_error'
        elif task.gradable:
            correct, why = grade(
                task.to_task(), text, env=self._config.grade_env
            )
        else:
            correct, why = None, 'ungraded'
        return {
            'arm': self._arm.key,
            'label': self._arm.label,
            'correct': correct,
            'grade': why,
            'cost_usd': result.get('total_cost_usd'),
            'wall_s': result.get('wall_s'),
            'answer': answer_line(text),
            'result': (text or '')[-MAX_RESULT_CHARS:],
            'model_usage': result.get('model_usage') or {},
            'subagents': result.get('subagents'),
            'is_error': failed,
            'error': error or result.get('stderr_tail'),
        }
