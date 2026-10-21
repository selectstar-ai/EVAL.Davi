# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Routing decisions, with the model to run next as a plain string.

A hook cannot switch the main Claude Code session's model, but a decision
can be carried as a model string and applied where a model is chosen per
call: the next ``Agent`` tool call (``tool_input.model``, set by the
PreToolUse hook) or the next ``claude -p --model <model>`` run.

- ``RoutePolicy``  two models: P(accept) -> accept / escalate.
- ``TierPolicy``   three models: difficulty level -> cheap / mid / strong.
"""

from __future__ import annotations

from dataclasses import dataclass

ON_ERROR = ('accept', 'escalate')
TIERS = ('cheap', 'mid', 'strong')


def same_model(current, target):
    """True when ``current`` names ``target``.

    'opus' matches 'opus' and 'claude-opus-5-5'; full ids match exactly.
    """
    if not current or not target:
        return False
    current, target = current.strip().lower(), target.strip().lower()
    return current == target or target in current or current in target


@dataclass(frozen=True)
class Route:
    """One decision. ``next_model`` is None when the result is kept."""

    decision: str  # 'accept' | 'escalate'
    next_model: str | None
    reason: str  # see RoutePolicy.decide


@dataclass(frozen=True)
class RoutePolicy:
    """Escalate to ``strong_model`` when P(accept) is below ``threshold``.

    A verifier failure keeps the cheap result by default (fail-open).
    """

    threshold: float = 0.5
    strong_model: str = 'opus'
    cheap_model: str = 'haiku'
    on_error: str = 'accept'

    def __post_init__(self):
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError('threshold must be in [0, 1]')
        if self.on_error not in ON_ERROR:
            raise ValueError(f'on_error must be one of {ON_ERROR}')

    def decide(self, p_accept, current_model=None):
        """Reasons: already_strong, verifier_error, p_above_threshold,
        p_below_threshold."""
        if current_model and same_model(current_model, self.strong_model):
            # nothing stronger to escalate to, so no loop
            return Route('accept', None, 'already_strong')
        if p_accept is None:
            if self.on_error == 'escalate':
                return Route('escalate', self.strong_model, 'verifier_error')
            return Route('accept', None, 'verifier_error')
        if p_accept >= self.threshold:
            return Route('accept', None, 'p_above_threshold')
        return Route('escalate', self.strong_model, 'p_below_threshold')


@dataclass(frozen=True)
class TierPolicy:
    """Pick one of three models from a difficulty level in [0, 1].

    Below ``low`` the cheap model, from ``high`` the strong model, the mid
    model in between. A missing level (a verifier failure) goes to the
    cheap model, or to the strong one when ``on_error`` is 'escalate'.
    """

    low: float = 0.48
    high: float = 0.78
    cheap_model: str = 'haiku'
    mid_model: str = 'sonnet'
    strong_model: str = 'opus'
    on_error: str = 'accept'

    def __post_init__(self):
        if not 0.0 <= self.low <= self.high <= 1.0:
            raise ValueError('cut-offs must satisfy 0 <= low <= high <= 1')
        if self.on_error not in ON_ERROR:
            raise ValueError(f'on_error must be one of {ON_ERROR}')

    def tier_of(self, level):
        """'cheap', 'mid' or 'strong'."""
        if level is None:
            return 'strong' if self.on_error == 'escalate' else 'cheap'
        if level < self.low:
            return 'cheap'
        if level >= self.high:
            return 'strong'
        return 'mid'

    def model_for(self, tier):
        """The model string of a tier."""
        return {
            'cheap': self.cheap_model,
            'mid': self.mid_model,
            'strong': self.strong_model,
        }[tier]
