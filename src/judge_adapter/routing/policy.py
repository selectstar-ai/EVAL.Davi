# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""P(accept) -> accept / escalate, with the model to use next as a plain string.

This is the "E" idea in a form Claude Code supports: a hook cannot switch the MAIN session's model, but the decision
can be carried as a model string and applied where a model can be chosen per call:

- inside Claude Code, to the next ``Agent`` tool call (``tool_input.model``), by the PreToolUse hook (``hooks.py``);
- in the headless harness, to the next ``claude -p --model <next_model>`` run (``harness.py``).
"""
from __future__ import annotations

from dataclasses import dataclass

ON_ERROR = ('accept', 'escalate')


@dataclass(frozen=True)
class Route:
    decision: str                 # 'accept' | 'escalate'
    next_model: str | None        # the model to run next; None = keep the current result
    reason: str                   # 'p_above_threshold' | 'p_below_threshold' | 'already_strong' | 'verifier_error'


def same_model(current, target):
    """'opus' matches 'opus' and 'claude-opus-5-5'; full ids match exactly."""
    if not current or not target:
        return False
    c, t = current.strip().lower(), target.strip().lower()
    return c == t or t in c or c in t


@dataclass(frozen=True)
class RoutePolicy:
    threshold: float = 0.5        # escalate when P(accept) < threshold
    strong_model: str = 'opus'
    cheap_model: str = 'haiku'
    on_error: str = 'accept'      # fail-open by default: a verifier failure keeps the cheap result

    def __post_init__(self):
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError('threshold must be in [0, 1]')
        if self.on_error not in ON_ERROR:
            raise ValueError(f'on_error must be one of {ON_ERROR}')

    def decide(self, p_accept, current_model=None):
        if current_model and same_model(current_model, self.strong_model):
            return Route('accept', None, 'already_strong')       # nothing stronger to escalate to: no loop
        if p_accept is None:
            if self.on_error == 'escalate':
                return Route('escalate', self.strong_model, 'verifier_error')
            return Route('accept', None, 'verifier_error')
        if p_accept >= self.threshold:
            return Route('accept', None, 'p_above_threshold')
        return Route('escalate', self.strong_model, 'p_below_threshold')
