# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Demo runs: ``DemoRun`` (abstract) -> ``LiveRun`` / ``ReplayRun``.

Both fill an ``EventChannel`` with the same event types, so the browser
draws a live run and a replay the same way.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod

from ..policy import TierPolicy
from ..verifiers import build_verifier
from .events import (
    ERROR,
    JUDGE,
    ROUTING,
    RUN_DONE,
    RUN_STARTED,
    SESSION_DONE,
    SESSION_STARTED,
    SUBAGENT,
    SUMMARY,
    EventChannel,
)
from .sessions import ClaudeSession
from .tasks import TIER_LABELS, opus_arm, routed_arm

JUDGE_LABELS = {'davi': 'Davi', 'jev': 'Jev', 'claude': 'Claude'}
REPLAYED_TYPES = frozenset(
    {
        RUN_STARTED,
        JUDGE,
        SESSION_STARTED,
        ROUTING,
        SUBAGENT,
        SESSION_DONE,
        SUMMARY,
        ERROR,
    }
)


class DemoRun(ABC):
    """One question on screen. ``start`` runs it in a thread; the channel
    ends with ``run_done`` whatever happens."""

    def __init__(self, run_id):
        self._channel = EventChannel(run_id)
        self._thread = None

    @property
    def run_id(self):
        return self._channel.run_id

    @property
    def channel(self):
        return self._channel

    @property
    def finished(self):
        return self._channel.closed

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self

    def run(self):
        """Run to the end in the calling thread."""
        extra = {}
        try:
            extra = self._run() or {}
        except Exception as error:  # noqa: BLE001 - shown on screen
            self._channel.emit(
                ERROR,
                {
                    'message': f'{type(error).__name__}: {error}'[:300],
                },
            )
        finally:
            self._channel.emit(RUN_DONE, extra)
            self._channel.close()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    @abstractmethod
    def _run(self):
        """Emit the run's events; return the ``run_done`` data."""


class JudgePanel:
    """Asks each judge how hard the question is (kind ``tier``)."""

    def __init__(self, judges, policy, verifier_factory):
        self._judges = tuple(judges)
        self._policy = policy
        self._factory = verifier_factory

    @property
    def judges(self):
        return self._judges

    def compare(self, question, channel):
        threads = [
            threading.Thread(
                target=self._ask, args=(name, question, channel), daemon=True
            )
            for name in self._judges
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def _ask(self, name, question, channel):
        verifier = self._factory(name)
        try:
            verdict = verifier.verify(question, kind='tier')
        finally:
            verifier.close()
        ok = verdict.outcome == 'ok' and verdict.level is not None
        tier = self._policy.tier_of(verdict.level) if ok else None
        channel.emit(
            JUDGE,
            {
                'judge': name,
                'label': JUDGE_LABELS.get(name, name),
                'ok': ok,
                'outcome': verdict.outcome,
                'level': verdict.level,
                'tier': tier,
                'tier_label': TIER_LABELS.get(tier),
                'model': self._policy.model_for(tier) if tier else None,
                'latency_s': round(verdict.latency_s, 3),
                'cost_usd': verdict.cost_usd,
            },
        )


class LiveRun(DemoRun):
    """Two real Claude Code sessions and the judge panel, recorded."""

    def __init__(
        self,
        run_id,
        task,
        config,
        store=None,
        judges=('davi',),
        policy=None,
        runner=None,
        verifier_factory=None,
    ):
        super().__init__(run_id)
        self._task = task
        self._policy = policy or TierPolicy()
        self._store = store
        factory = verifier_factory or (
            lambda name: build_verifier(name, config.env)
        )
        self._panel = JudgePanel(judges, self._policy, factory)
        session_args = {} if runner is None else {'runner': runner}
        self._sessions = [
            ClaudeSession(routed_arm(self._policy), config, **session_args),
            ClaudeSession(opus_arm(self._policy), config, **session_args),
        ]

    @property
    def task(self):
        return self._task

    def _run(self):
        policy = self._policy
        self._channel.emit(
            RUN_STARTED,
            {
                'mode': 'live',
                'task': self._task.to_dict(),
                'arms': [
                    {'key': s.arm.key, 'label': s.arm.label}
                    for s in self._sessions
                ],
                'tiers': {
                    'low': policy.low,
                    'high': policy.high,
                    'models': {
                        tier: policy.model_for(tier)
                        for tier in ('cheap', 'mid', 'strong')
                    },
                    'labels': TIER_LABELS,
                },
                'judges': [
                    {'judge': name, 'label': JUDGE_LABELS.get(name, name)}
                    for name in self._panel.judges
                ],
            },
        )
        results = {}
        workers = [
            threading.Thread(
                target=self._panel.compare,
                args=(self._task.question, self._channel),
                daemon=True,
            )
        ]
        workers += [
            threading.Thread(
                target=self._run_session,
                args=(session, results),
                daemon=True,
            )
            for session in self._sessions
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self._channel.emit(SUMMARY, summarize(self._channel.events()))
        if self._store is None:
            return {}
        recording_id = self._store.save(self._task, self._channel.events())
        return {'recording_id': recording_id}

    def _run_session(self, session, results):
        try:
            results[session.arm.key] = session.run(self._task, self._channel)
        except Exception as error:  # noqa: BLE001 - shown on screen
            self._channel.emit(
                ERROR,
                {
                    'arm': session.arm.key,
                    'message': f'{type(error).__name__}: {error}'[:300],
                },
            )


class ReplayRun(DemoRun):
    """A recorded run, re-emitted with its timing (``speed`` x faster)."""

    def __init__(self, run_id, recording, speed=1.0, sleep=time.sleep):
        super().__init__(run_id)
        if speed <= 0:
            raise ValueError('speed must be positive')
        self._recording = recording
        self._speed = speed
        self._sleep = sleep

    def _run(self):
        previous = 0.0
        for event in self._recording.get('events') or []:
            event_type = event.get('type')
            if event_type not in REPLAYED_TYPES:
                continue
            t = float(event.get('t') or 0.0)
            if t > previous:
                self._sleep((t - previous) / self._speed)
                previous = t
            data = dict(event.get('data') or {})
            if event_type == RUN_STARTED:
                data.update(
                    mode='replay',
                    recording_id=self._recording.get('id'),
                    recorded_at=self._recording.get('recorded_at'),
                    speed=self._speed,
                )
            self._channel.emit(event_type, data, t=t)
        return {'recording_id': self._recording.get('id'), 'replay': True}


def summarize(events):
    """Routed vs always-opus, from a run's events."""
    done, routed = {}, {}
    for event in events:
        data = event.data
        if event.type == SESSION_DONE:
            done[data.get('arm')] = data
        elif event.type == ROUTING and data.get('arm') == 'routed':
            routed['model'] = data.get('model_set') or routed.get('model')
            routed['level'] = data.get('level', routed.get('level'))
            routed['tier'] = data.get('tier') or routed.get('tier')
        elif event.type == SUBAGENT and data.get('arm') == 'routed':
            routed['resolved'] = data.get('resolved_model')
    fast, strong = done.get('routed') or {}, done.get('opus') or {}
    fast_cost, strong_cost = fast.get('cost_usd'), strong.get('cost_usd')
    saving = saving_pct = None
    if _is_number(fast_cost) and _is_number(strong_cost):
        saving = strong_cost - fast_cost
        saving_pct = saving / strong_cost if strong_cost else None
    return {
        'routed_model': routed.get('model'),
        'routed_level': routed.get('level'),
        'routed_tier': routed.get('tier'),
        'routed_resolved_model': routed.get('resolved'),
        'routed_cost_usd': fast_cost,
        'opus_cost_usd': strong_cost,
        'saving_usd': saving,
        'saving_pct': saving_pct,
        'routed_correct': fast.get('correct'),
        'opus_correct': strong.get('correct'),
        'routed_wall_s': fast.get('wall_s'),
        'opus_wall_s': strong.get('wall_s'),
    }


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)
