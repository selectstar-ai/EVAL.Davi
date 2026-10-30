# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Questions, answers and errors of the decision core.

A question knows how to put itself on the wire and how to read its answer
back, so the client never branches on the question type:

    Question (abstract)
    ├── ChoiceQuestion   one of 2..N options, per-option probabilities
    └── NoulQuestion     P(true) of a statement, sent one of two ways
                         ('choice': a true/false choice question,
                          'native': the server's ``noul`` type)

Reading is strict: probabilities must be finite numbers in [0, 1] (not
bools), cover exactly the asked options and sum to 1 within ``SUM_TOLERANCE``.
Nothing is renormalised or filled in; a bad answer is a ``format_error``.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

ERROR_CODES = (
    'invalid_request',
    'credential_missing',
    'timeout',
    'provider_error',
    'format_error',
)
NOUL_MODES = ('choice', 'native')
SUM_TOLERANCE = 1e-6
NOUL_KEYS = ('true', 'false')


class DecisionError(Exception):
    """A failed decision. ``message`` never holds state, answer or key text."""

    def __init__(self, code, message):
        if code not in ERROR_CODES:
            raise ValueError(f'unknown error code {code!r}')
        super().__init__(f'{code}: {message}')
        self._code = code
        self._message = message

    @property
    def code(self):
        return self._code

    @property
    def message(self):
        return self._message


def _invalid(message):
    return DecisionError('invalid_request', message)


def _bad_format(message):
    return DecisionError('format_error', message)


def _text(value, what):
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f'{what} must be a non-empty string')
    return value


def _probability(value, what):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise _bad_format(f'{what} is not a probability in [0, 1]')
    return float(value)


def _distribution(raw, keys, what):
    """``raw`` as {key: probability} over exactly ``keys``."""
    if not isinstance(raw, dict):
        raise _bad_format(f'{what} has no probabilities')
    if set(raw) != set(keys):
        raise _bad_format(f'{what} probabilities do not match the options')
    shares = {key: _probability(raw[key], what) for key in keys}
    if abs(sum(shares.values()) - 1.0) > SUM_TOLERANCE:
        raise _bad_format(f'{what} probabilities do not sum to 1')
    return shares


def _best(shares, order):
    """The most probable key; ties go to the first key in ``order``."""
    top = max(shares.values())
    return next(key for key in order if shares[key] == top)


@dataclass(frozen=True)
class ChoiceAnswer:
    value: str
    probabilities: dict

    def to_dict(self):
        return {
            'type': 'choice',
            'value': self.value,
            'probabilities': dict(self.probabilities),
        }


@dataclass(frozen=True)
class NoulAnswer:
    value: bool
    p_true: float
    threshold: float
    mode: str

    def to_dict(self):
        return {
            'type': 'noul',
            'value': self.value,
            'p_true': self.p_true,
            'threshold': self.threshold,
            'noul_mode': self.mode,
        }


class Question(ABC):
    """One question about a state. Subclasses validate on construction."""

    def __init__(self, instructions):
        self._instructions = _text(instructions, 'instructions')

    @property
    def instructions(self):
        return self._instructions

    @property
    @abstractmethod
    def kind(self):
        """'choice' or 'noul'."""

    @abstractmethod
    def option_count(self, noul_mode):
        """How many options the server sees (0 for a native noul)."""

    @abstractmethod
    def wire(self, noul_mode):
        """The question object for ``POST /v1/systemone``."""

    @abstractmethod
    def read(self, answer, noul_mode, what='answer'):
        """The answer object from the response -> an answer record."""


class ChoiceQuestion(Question):
    """Pick one of ``options`` ({id: description}, insertion order kept)."""

    kind = 'choice'

    def __init__(self, instructions, options):
        super().__init__(instructions)
        if not isinstance(options, dict) or len(options) < 2:
            raise _invalid('a choice needs at least two options')
        for key, description in options.items():
            _text(key, 'an option id')
            _text(description, f'option {key!r}')
        self._options = dict(options)

    @property
    def options(self):
        return dict(self._options)

    def option_count(self, noul_mode):
        return len(self._options)

    def wire(self, noul_mode):
        return {
            'type': 'choice',
            'instructions': self.instructions,
            'criteria': dict(self._options),
        }

    def read(self, answer, noul_mode, what='answer'):
        if not isinstance(answer, dict):
            raise _bad_format(f'{what} is missing')
        order = list(self._options)
        shares = _distribution(answer.get('probabilities'), order, what)
        best = _best(shares, order)
        given = answer.get('choice')
        if given is not None and (
            given not in shares or shares[given] < shares[best]
        ):
            raise _bad_format(f'{what} choice is not the most probable')
        return ChoiceAnswer(best, shares)


class NoulQuestion(Question):
    """P(the statement in ``instructions`` is true about the state).

    ``criteria`` is {'true': ..., 'false': ...}. The 'choice' mode sends it
    as the two options; the 'native' mode sends the server's ``noul`` type
    without criteria. ``value`` is ``p_true >= threshold``.
    """

    kind = 'noul'

    def __init__(self, instructions, criteria, threshold=0.5):
        super().__init__(instructions)
        if not isinstance(criteria, dict) or set(criteria) != set(NOUL_KEYS):
            raise _invalid("a noul needs exactly 'true' and 'false' criteria")
        for key in NOUL_KEYS:
            _text(criteria[key], f'criterion {key!r}')
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or not 0.0 <= threshold <= 1.0
        ):
            raise _invalid('threshold must be a number in [0, 1]')
        self._criteria = {key: criteria[key] for key in NOUL_KEYS}
        self._threshold = float(threshold)

    @property
    def criteria(self):
        return dict(self._criteria)

    @property
    def threshold(self):
        return self._threshold

    def option_count(self, noul_mode):
        return 2 if noul_mode == 'choice' else 0

    def wire(self, noul_mode):
        if noul_mode == 'native':
            return {'type': 'noul', 'instructions': self.instructions}
        return {
            'type': 'choice',
            'instructions': self.instructions,
            'criteria': dict(self._criteria),
        }

    def read(self, answer, noul_mode, what='answer'):
        if not isinstance(answer, dict):
            raise _bad_format(f'{what} is missing')
        if noul_mode == 'native':
            p_true = _probability(answer.get('noul'), what)
        else:
            shares = _distribution(
                answer.get('probabilities'), NOUL_KEYS, what
            )
            p_true = shares['true']
        return NoulAnswer(
            p_true >= self._threshold, p_true, self._threshold, noul_mode
        )


@dataclass(frozen=True)
class Reply:
    """The answers of one request, keyed like the questions."""

    answers: dict
    provider: str
    model: Optional[str]
    latency_ms: float
    usage: Optional[dict] = None
    meta: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            'answers': {
                key: answer.to_dict() for key, answer in self.answers.items()
            },
            'meta': {
                'provider': self.provider,
                'model': self.model,
                'latency_ms': self.latency_ms,
                'usage': self.usage,
                **self.meta,
            },
        }
