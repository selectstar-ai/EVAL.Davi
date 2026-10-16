# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""The questions every judge is asked, in one place.

Kinds:

- ``answer``      can the cheap model's answer be used as final, or should a
                  stronger model redo the task?
- ``completion``  is the agent's task actually complete, or should it go on?
- ``pre``         from the task alone, before any answer: will a small model
                  get it right, or should the task go to the strong model?
- ``tier``        from the task alone: how hard is it for a small model, on
                  five levels (for routing over three models).

In the binary kinds option ``accept`` means keep / stop / send to the cheap
model and option ``escalate`` means redo / continue / send to the strong
model. ``tier`` is graded: the judge spreads probability over five levels.
"""

from __future__ import annotations

import json
import math

QUESTION_ID = 'route'
LABELS = ('accept', 'escalate')
DEFAULT_MAX_CHARS = 24_000

KINDS = {
    'answer': {
        'instructions': (
            'A lower-cost AI model produced the answer in the field '
            '"candidate" for the task in the field "task". Decide whether '
            'this answer can be used as the final answer as it is: it must '
            "be correct, complete and follow the task's instructions. If "
            'the field "rubric" is present, apply it. Choose "accept" only '
            'when the answer is acceptable. Choose "escalate" when it may be '
            'wrong, incomplete, unsupported or off-task, so that a stronger '
            'model solves the task again.'
        ),
        'accept': (
            'Accept: the answer is correct and complete; use it as the '
            'final answer.'
        ),
        'escalate': (
            'Escalate: the answer may be wrong or incomplete; a stronger '
            'model should solve the task again.'
        ),
    },
    'completion': {
        'instructions': (
            'An AI agent is about to stop working on the user request in '
            'the field "task". Its final message is in the field '
            '"candidate". Decide whether the requested task is actually '
            'complete: the requested final state is reached, no requested '
            'step is left undone, and the agent did not stop early or claim '
            'success without doing the work. If the field "rubric" is '
            'present, apply it. Choose "accept" only when the task is '
            'complete. Choose "escalate" when it is not complete or '
            'completion is not shown.'
        ),
        'accept': 'Complete: the requested task is done; the agent can stop.',
        'escalate': (
            'Not complete: something requested is missing or unverified; '
            'the agent should continue.'
        ),
    },
    # "Will a small model answering from memory get it right?" separated
    # haiku-right from haiku-wrong tasks better than asking about the task's
    # difficulty directly (KLEAR, 2026-10-15; docs/routing.md).
    'pre': {
        'instructions': (
            'A small, fast AI model will answer the task in the field "task" '
            'from memory, with no tools or search, before any stronger model '
            'sees it. Will its final answer be correct? If the field '
            '"rubric" is present, apply it. Choose "accept" if it will be '
            'correct, so the task goes to the small model. Choose "escalate" '
            'if it will likely be wrong, so the task goes to the stronger '
            'model directly.'
        ),
        'accept': (
            'Yes: a small model answering from memory will get it right; '
            'send the task to it.'
        ),
        'escalate': (
            'No: a small model will likely get it wrong; send the task to '
            'the stronger model directly.'
        ),
    },
    # The wording of the KLEAR experiment that set the default cut-offs.
    'tier': {
        'instructions': (
            'Rate how hard the task in "task" is for a small, fast AI model '
            'answering from memory with no tools.'
        ),
    },
}
TASK_ONLY_KINDS = ('pre', 'tier')
TIER_LEVELS = (
    ('1', '1: trivial for it.'),
    ('2', '2: easy.'),
    ('3', '3: uncertain, about even odds.'),
    ('4', '4: hard, likely wrong.'),
    ('5', '5: very hard, almost surely wrong.'),
)

# Several phrasings of the ``pre`` question, asked in ONE /v1/systemone
# call; P(accept) is the mean of their logits (no weights are fitted).
# name -> (instructions, {option: description}, option meaning "accept")
PRE_ENSEMBLE = {
    'memory': (
        'A small, fast AI model will answer the question in "task" from '
        'memory, with no tools or search. Will its final answer be correct?',
        {
            'yes': 'Yes: a small model answering from memory will get it '
            'right.',
            'no': 'No: a small model answering from memory will likely get '
            'it wrong.',
        },
        'yes',
    ),
    'memory_ko': (
        '작고 빠른 AI 모델이 도구나 검색 없이 기억만으로 "task"의 질문에 '
        '답합니다. 최종 답이 맞을까요?',
        {
            'yes': '예: 기억만으로 답해도 맞힐 것이다.',
            'no': '아니오: 기억만으로는 틀릴 가능성이 높다.',
        },
        'yes',
    ),
    'closed_book': (
        'Treat "task" as a closed-book quiz question. Would a typical small '
        'language model give exactly the right final answer?',
        {
            'yes': 'Yes: it would get the exact final answer.',
            'no': 'No: it would miss the exact answer.',
        },
        'yes',
    ),
    'confidence': (
        'How confident can we be that a small AI model, answering "task" '
        'immediately without tools, gives the correct final answer?',
        {
            'high': 'High confidence: it will be correct.',
            'low': 'Low confidence: it may well be wrong.',
        },
        'high',
    ),
    'easy': (
        'Is the question in "task" easy enough that a small AI model '
        'answering from memory would get the exact answer?',
        {
            'yes': 'Yes: easy enough for a small model.',
            'no': 'No: too hard for a small model.',
        },
        'yes',
    ),
    'named': (
        'Claude Haiku, a small and fast model, will answer the question in '
        '"task" with no tools or search. Will its final answer be correct?',
        {
            'yes': 'Yes: Haiku will get it right.',
            'no': 'No: Haiku will likely get it wrong.',
        },
        'yes',
    ),
    'most_wrong': (
        'Would most small AI models answer the question in "task" '
        'incorrectly?',
        {
            'no': 'No: most small models would answer it correctly.',
            'yes': 'Yes: most small models would get it wrong.',
        },
        'no',
    ),
    'popular': (
        'Is the answer to the question in "task" widely documented and '
        'frequently repeated, so that it is likely memorised by language '
        'models?',
        {
            'yes': 'Yes: widely documented and likely memorised.',
            'no': 'No: obscure or rarely written down.',
        },
        'yes',
    ),
}

CLAUDE_FORMAT_LINE = (
    'Reply with only an integer from 0 to 100: your probability, in '
    'percent, that "accept" is the right option. No other text.'
)
CLAUDE_TIER_FORMAT_LINE = (
    'Reply with only one integer from 1 to 5: the rating. No other text.'
)


def clip(text, max_chars=DEFAULT_MAX_CHARS):
    """Keep the head and the tail of a long text (outputs end with the
    conclusion)."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        f'{text[:head]}\n[... {omitted} characters omitted ...]\n'
        f'{text[-tail:]}'
    )


def state_of(task, candidate, rubric=None, max_chars=DEFAULT_MAX_CHARS):
    """The judged state; ``candidate=None`` (a task-only kind) leaves the
    field out."""
    state = {'task': clip(task or '', max_chars)}
    if candidate is not None:
        state['candidate'] = clip(candidate, max_chars)
    if rubric:
        state['rubric'] = clip(rubric, max_chars)
    return state


def tier_level(probabilities):
    """Expected level in [0, 1] from ``{'1': p, ..., '5': p}``.

    None when a level is missing or the probabilities are unusable.
    """
    if not isinstance(probabilities, dict):
        return None
    values = [probabilities.get(level) for level, _ in TIER_LEVELS]
    for value in values:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if value < 0:
            return None
    total = sum(values)
    if total <= 0:
        return None
    steps = len(values) - 1
    return sum(
        value / total * index / steps for index, value in enumerate(values)
    )


def level_shares(probabilities):
    """``{'1': p, ..., '5': p}`` normalized to sum 1, rounded; None when
    unusable."""
    if tier_level(probabilities) is None:
        return None
    total = sum(probabilities[level] for level, _ in TIER_LEVELS)
    return {
        level: round(probabilities[level] / total, 4)
        for level, _ in TIER_LEVELS
    }


def mean_logit_probability(probabilities):
    """The probability whose logit is the mean logit of ``probabilities``."""
    logits = []
    for p in probabilities:
        p = min(max(float(p), 1e-6), 1 - 1e-6)
        logits.append(math.log(p / (1 - p)))
    return 1 / (1 + math.exp(-sum(logits) / len(logits)))
