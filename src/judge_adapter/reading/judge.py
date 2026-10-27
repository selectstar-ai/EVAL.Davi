# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Rate how far each block answers a question, with the decision core.

Pass 1 puts a window of blocks in one request, one noul question per block.
The state starts with the query and the window, and the questions come
after it, so a server with prefix caching reads the window once.
Pass 2 asks again about each candidate alone, with the neighbouring text
as context; those requests run concurrently.

The condition that a qualifying rule, limit or exception counts as part of
the answer is written into the question itself: Davi's native noul does not
use criteria (engineering_plan/05).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..decision import NoulQuestion

PASS1 = (
    'Does passage {pid} in `passages` state something that answers the '
    'question in `query`, or one part of it? A value, limit, rule, '
    'condition, exception, example or behaviour that bears on the answer '
    'counts. A passage that only names the topic does not.'
)
PASS2 = (
    'Does `passage` state something that answers the question in `query`, '
    'or one part of it? A value, limit, rule, condition, exception, example '
    'or behaviour that bears on the answer counts. A passage that only '
    'names the topic does not. `before` and `after` are the neighbouring '
    'text, shown only to make clear what `passage` refers to.'
)
CRITERIA = {
    'true': 'It states part of the answer, or a condition or exception '
    'that qualifies the answer.',
    'false': 'It does not; at most it names the topic.',
}


class RelevanceJudge:
    """``first_pass`` and ``second_pass`` return {block id: P(relevant)}."""

    def __init__(
        self,
        client,
        *,
        window_chars=9000,
        window_blocks=60,
        context_chars=400,
        concurrency=8,
    ):
        self._client = client
        self._window_chars = window_chars
        self._window_blocks = window_blocks
        self._context_chars = context_chars
        self._concurrency = concurrency
        self._requests = 0

    @property
    def requests(self):
        return self._requests

    def windows(self, blocks):
        """Consecutive blocks, at most ``window_chars`` and
        ``window_blocks`` each (a single long block gets its own)."""
        out, current, size = [], [], 0
        for block in blocks:
            cost = len(block.text) + len(block.section) + 20
            if current and (
                size + cost > self._window_chars
                or len(current) >= self._window_blocks
            ):
                out.append(current)
                current, size = [], 0
            current.append(block)
            size += cost
        if current:
            out.append(current)
        return out

    def first_pass(self, blocks, query):
        return self._merge(
            self._run(lambda window: self._window(window, query), blocks)
        )

    def second_pass(self, blocks, ids, query):
        by_id = {block.id: index for index, block in enumerate(blocks)}
        targets = [blocks[by_id[i]] for i in ids]
        return self._merge(
            self._run(
                lambda block: self._alone(blocks, by_id, block, query),
                targets,
                group=False,
            )
        )

    # --- requests ------------------------------------------------------------
    def _window(self, window, query):
        state = {
            'query': query,
            'passages': [
                {'id': f'p{b.id}', 'section': b.section, 'text': b.text}
                for b in window
            ],
        }
        questions = {
            f'p{b.id}': NoulQuestion(PASS1.format(pid=f'p{b.id}'), CRITERIA)
            for b in window
        }
        reply = self._client.ask(state, questions)
        return {int(key[1:]): a.p_true for key, a in reply.answers.items()}

    def _alone(self, blocks, by_id, block, query):
        index = by_id[block.id]
        before = blocks[index - 1].text if index > 0 else ''
        after = blocks[index + 1].text if index + 1 < len(blocks) else ''
        state = {
            'query': query,
            'section': block.section,
            'passage': block.text,
            'before': before[-self._context_chars :],
            'after': after[: self._context_chars],
        }
        answer = self._client.decide(state, NoulQuestion(PASS2, CRITERIA))
        return {block.id: answer.p_true}

    def _run(self, work, items, group=True):
        units = self.windows(items) if group else list(items)
        if not units:
            return []
        self._requests += len(units)
        if len(units) == 1:
            return [work(units[0])]
        with ThreadPoolExecutor(
            max_workers=min(self._concurrency, len(units))
        ) as pool:
            return list(pool.map(work, units))

    @staticmethod
    def _merge(parts):
        scores = {}
        for part in parts:
            scores.update(part)
        return scores
