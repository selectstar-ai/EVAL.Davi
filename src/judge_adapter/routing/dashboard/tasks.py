# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Demo questions and the two Claude Code session setups."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..harness import Task
from ..policy import TierPolicy

# the user's phrasing: the main agent writes the subagent prompt itself,
# and the PreToolUse hook rates that prompt
PROMPT_TEMPLATE = (
    'Agent 도구로 서브에이전트에게 맡겨서 풀어줘: {question}\n\n'
    '서브에이전트가 낸 최종 답을 바꾸지 말고, 마지막 줄에 "정답: <답>" '
    '형식으로 그대로 적어줘.'
)
TIER_LABELS = {
    'cheap': '가벼운 모델',
    'mid': '중간 모델',
    'strong': '고성능 모델',
}
# a free question typed on the dashboard
FREE_TASK_ID = 'free'


@dataclass(frozen=True)
class DemoTask:
    """One demo question. ``answer`` + ``grader`` make it gradable."""

    id: str
    question: str
    title: str = ''
    answer: str | None = None
    grader: str | None = None
    note: str = ''

    @property
    def gradable(self):
        return self.answer is not None

    def prompt(self):
        """What the user types into Claude Code."""
        return PROMPT_TEMPLATE.format(question=self.question)

    def to_task(self):
        """The harness task (for grading)."""
        return Task(
            self.id, self.prompt(), answer=self.answer, grader=self.grader
        )

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'question': self.question,
            'answer': self.answer,
            'gradable': self.gradable,
            'note': self.note,
        }

    @classmethod
    def from_dict(cls, data):
        question = data.get('question') or data.get('prompt')
        if not data.get('id') or not question:
            raise ValueError('a demo task needs an id and a question')
        return cls(
            id=str(data['id']),
            question=str(question),
            title=str(data.get('title') or data.get('topic') or ''),
            answer=data.get('answer'),
            grader=data.get('grader'),
            note=str(data.get('note') or data.get('scenario') or ''),
        )

    @classmethod
    def free(cls, question):
        """A question typed on the dashboard: not graded."""
        text = (question or '').strip()
        if not text:
            raise ValueError('the question is empty')
        return cls(FREE_TASK_ID, text, title='직접 입력한 질문')


def load_demo_tasks(path):
    """Demo tasks of a JSONL file (``id``, ``question`` or ``prompt``, and
    optionally ``title``, ``answer``, ``grader``, ``note``)."""
    tasks, seen = [], set()
    with open(path, encoding='utf-8') as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                task = DemoTask.from_dict(json.loads(line))
            except ValueError as error:
                raise ValueError(f'{path}:{number}: {error}') from error
            if task.id in seen:
                raise ValueError(f'{path}:{number}: duplicate id {task.id}')
            seen.add(task.id)
            tasks.append(task)
    return tasks


@dataclass(frozen=True)
class ArmSpec:
    """One Claude Code session setup: a key, a label and plugin options."""

    key: str
    label: str
    options: dict = field(default_factory=dict)
    needs_davi: bool = False


def routed_arm(policy=None):
    """The plugin picks haiku, sonnet or opus from Davi's difficulty."""
    policy = policy or TierPolicy()
    return ArmSpec(
        key='routed',
        label='Davi 라우팅',
        options={
            'ROUTE_ENABLED': 'true',
            'ROUTE_MODE': 'tiers',
            'ROUTE_JUDGE': 'davi',
            'ROUTE_TIER_LOW': str(policy.low),
            'ROUTE_TIER_HIGH': str(policy.high),
            'ROUTE_CHEAP_MODEL': policy.cheap_model,
            'ROUTE_MID_MODEL': policy.mid_model,
            'ROUTE_STRONG_MODEL': policy.strong_model,
        },
        needs_davi=True,
    )


def opus_arm(policy=None):
    """Every subagent pinned to the strong model; no judge is called."""
    strong = (policy or TierPolicy()).strong_model
    return ArmSpec(
        key='opus',
        label='항상 opus',
        options={
            'ROUTE_ENABLED': 'true',
            'ROUTE_MODE': 'post',
            'ROUTE_JUDGE': 'fake',
            'ROUTE_PIN_CHEAP': 'always',
            'ROUTE_CHEAP_MODEL': strong,
            'ROUTE_STRONG_MODEL': strong,
        },
    )
