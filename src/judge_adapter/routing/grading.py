# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Graders: is a model's answer right? -> (True / False / None, detail).

Grader (abstract)
├── NoGrader              no reference: ungraded
├── CommandGrader         a shell command reads the answer on stdin
├── LlmGrader             a Claude model compares with the reference
└── ReferenceGrader (abstract)  compares the answer text with the reference
    ├── ExactGrader
    ├── ContainsGrader
    ├── RegexGrader
    └── NumberGrader      also reads Korean amounts ('21억 3,000만')

Answers are read without markdown emphasis (``**B**`` -> ``B``). ``None``
means ungraded (no reference, or the grader failed); an empty answer is
wrong.
"""

from __future__ import annotations

import os
import re
import subprocess
from abc import ABC, abstractmethod

from .config import SECRET_ENVS, lazy_key, opt
from .questions import clip
from .verifiers import ANTHROPIC_VERSION, CLAUDE_BASE_URL, uses_effort

GRADERS = ('contains', 'exact', 'regex', 'number', 'cmd', 'llm', 'none')
# grading defines the labels: a stronger model than the judges
GRADER_DEFAULT_MODEL = 'claude-sonnet-5-5'
LLM_GRADER_PROMPT = (
    'Decide whether the response gives the same final answer as the '
    'reference answer. Ignore formatting, explanation and wording; judge '
    'only the final answer. Reply with exactly "yes" or "no".\n\n'
    '<question>\n{q}\n</question>\n\n<reference>\n{a}\n</reference>\n\n'
    '<response>\n{r}\n</response>'
)

_NUMBER_RE = re.compile(r'-?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?')
_EMPHASIS_RE = re.compile(r'\*\*|__|`')
_KOREAN_UNITS = {'조': 10**12, '억': 10**8, '만': 10**4}
_KOREAN_AMOUNT_RE = re.compile(
    r'(?:\d[\d,]*(?:\.\d+)?\s*[조억만]\s*)+(?:\d[\d,]*(?:\.\d+)?)?'
)
_KOREAN_PART_RE = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*([조억만]?)')
_YES_NO_RE = re.compile(r'\b(yes|no)\b')


def plain_text(text):
    """The answer without markdown emphasis, as the model meant it."""
    return _EMPHASIS_RE.sub('', str(text))


def normalized(text):
    return ' '.join(str(text).casefold().split())


def last_number(text):
    numbers = _NUMBER_RE.findall(text or '')
    return float(numbers[-1].replace(',', '')) if numbers else None


def last_korean_amount(text):
    """The last Korean-unit amount ('21억 3,000만' -> 2130000000.0)."""
    found = _KOREAN_AMOUNT_RE.findall(text or '')
    if not found:
        return None
    total = 0.0
    for number, unit in _KOREAN_PART_RE.findall(found[-1]):
        total += float(number.replace(',', '')) * _KOREAN_UNITS.get(unit, 1)
    return total


class Grader(ABC):
    """Grades one answer of one task."""

    @abstractmethod
    def check(self, task, result):
        """Return (correct: bool | None, detail). ``result`` is non-empty."""


class NoGrader(Grader):
    def check(self, task, result):
        return None, 'ungraded'


class CommandGrader(Grader):
    """``task.check_cmd`` gets the answer on stdin; exit 0 = correct."""

    def check(self, task, result):
        try:
            process = subprocess.run(
                task.check_cmd,
                shell=True,
                input=str(result),
                capture_output=True,
                text=True,
                cwd=task.cwd,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return None, 'cmd timeout'
        return process.returncode == 0, f'cmd exit {process.returncode}'


class ReferenceGrader(Grader):
    """Compares the plain answer text with ``task.answer``."""

    name = 'reference'

    def check(self, task, result):
        if task.answer is None:
            return None, 'no answer'
        return self._compare(str(task.answer), plain_text(result))

    @abstractmethod
    def _compare(self, reference, text):
        """Return (correct, detail)."""


class ExactGrader(ReferenceGrader):
    def _compare(self, reference, text):
        return normalized(text) == normalized(reference), 'exact'


class ContainsGrader(ReferenceGrader):
    def _compare(self, reference, text):
        return normalized(reference) in normalized(text), 'contains'


class RegexGrader(ReferenceGrader):
    def _compare(self, reference, text):
        found = re.search(reference, text, re.I | re.S)
        return found is not None, 'regex'


class NumberGrader(ReferenceGrader):
    """The last number (or Korean amount) of the answer, with a relative
    tolerance of 1e-6."""

    def _compare(self, reference, text):
        wanted = last_number(reference)
        candidates = [
            value
            for value in (last_number(text), last_korean_amount(text))
            if value is not None
        ]
        if not candidates or wanted is None:
            return False, 'number missing'
        tolerance = max(1e-9, 1e-6 * abs(wanted))
        hit = any(abs(value - wanted) <= tolerance for value in candidates)
        return hit, 'number'


class LlmGrader(Grader):
    """A Claude model decides whether the answer matches the reference."""

    def __init__(self, env, transport=None, model=None):
        self._env = env
        self._transport = transport
        self._model = model or opt(env, 'grader_model', GRADER_DEFAULT_MODEL)

    def check(self, task, result):
        import httpx

        key = lazy_key(self._env, SECRET_ENVS['claude'])()
        base_url = opt(self._env, 'claude_base_url', CLAUDE_BASE_URL)
        transport = self._transport or httpx.HTTPTransport(retries=0)
        with httpx.Client(transport=transport, timeout=120) as client:
            response = client.post(
                base_url + '/v1/messages',
                json=self._body(task, result),
                headers={
                    'x-api-key': key,
                    'anthropic-version': ANTHROPIC_VERSION,
                },
            )
        if response.status_code != 200:
            return None, f'grader HTTP {response.status_code}'
        blocks = response.json().get('content') or []
        text = ''.join(block.get('text', '') for block in blocks)
        found = _YES_NO_RE.findall(text.strip().lower())
        if found:  # the last yes/no: a reason may come first
            return found[-1] == 'yes', f'llm:{found[-1]}'
        return None, f'llm:unparsed {text.strip().lower()[:20]!r}'

    def _body(self, task, result):
        prompt = LLM_GRADER_PROMPT.format(
            q=clip(task.prompt, 8000), a=task.answer, r=clip(result, 16000)
        )
        body = {
            'model': self._model,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if uses_effort(self._model):
            body.update(max_tokens=2048, output_config={'effort': 'low'})
        else:
            body.update(max_tokens=4, temperature=0.0)
        return body


_REFERENCE_GRADERS = {
    'exact': ExactGrader,
    'contains': ContainsGrader,
    'regex': RegexGrader,
    'number': NumberGrader,
}


def grader_for(name, env=None, transport=None):
    """The grader object for a grader name (see ``GRADERS``)."""
    if name == 'none':
        return NoGrader()
    if name == 'cmd':
        return CommandGrader()
    if name == 'llm':
        return LlmGrader(env if env is not None else os.environ, transport)
    return _REFERENCE_GRADERS[name]()


def grade(task, result, env=None, transport=None):
    """(correct: bool | None, detail) for ``result`` of ``task``."""
    name = task.grader_name
    if name == 'none':
        return None, 'ungraded'
    if result is None or not str(result).strip():
        return False, 'empty'
    grader = grader_for(name, env, transport)
    if name != 'llm':
        return grader.check(task, result)
    try:
        return grader.check(task, str(result))
    except Exception as error:  # noqa: BLE001 - grading never raises
        return None, f'llm grader error {type(error).__name__}'
