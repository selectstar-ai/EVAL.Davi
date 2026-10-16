# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""An append-only event channel: one per run, many readers."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# event types, in the order a run usually emits them
RUN_STARTED = 'run_started'
JUDGE = 'judge'
SESSION_STARTED = 'session_started'
ROUTING = 'routing'
SUBAGENT = 'subagent'
SESSION_DONE = 'session_done'
SUMMARY = 'summary'
ERROR = 'error'
RUN_DONE = 'run_done'


@dataclass(frozen=True)
class Event:
    """One thing that happened, ``t`` seconds after the run started."""

    seq: int
    t: float
    type: str
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            'seq': self.seq,
            't': self.t,
            'type': self.type,
            'data': self.data,
        }


class EventChannel:
    """Events of one run. Writers ``emit``; readers ``read`` and wait."""

    def __init__(self, run_id, clock=time.monotonic):
        self._run_id = run_id
        self._clock = clock
        self._started = clock()
        self._events = []
        self._closed = False
        self._condition = threading.Condition()

    @property
    def run_id(self):
        return self._run_id

    @property
    def closed(self):
        with self._condition:
            return self._closed

    def emit(self, event_type, data=None, t=None):
        """Append an event; ``t`` overrides the elapsed time (replays)."""
        with self._condition:
            if self._closed:
                raise RuntimeError(f'run {self._run_id} is closed')
            elapsed = self._clock() - self._started if t is None else t
            event = Event(
                len(self._events) + 1,
                round(elapsed, 3),
                event_type,
                dict(data or {}),
            )
            self._events.append(event)
            self._condition.notify_all()
            return event

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def events(self):
        """A snapshot of every event so far."""
        with self._condition:
            return list(self._events)

    def read(self, after=0, timeout=15.0):
        """Events with ``seq > after``; waits up to ``timeout`` for one.

        Returns an empty list on a timeout or when the channel is closed
        with nothing new.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self._events) <= after and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self._events[after:]
