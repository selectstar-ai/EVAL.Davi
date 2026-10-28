# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Rate how far each block answers a question, with the decision core.

Two passes, the same method as jevfetch:

- Pass 1 groups consecutive blocks into windows (about ``window_tokens``
  and at most ``window_blocks`` each) and sends one request per window,
  with one noul question per block. The state starts with the query and
  the window, so a server with prefix caching reads the window once.
- Pass 2 asks again about each candidate (pass 1 at or above the candidate
  threshold) alone, with up to ``context_chars`` of the neighbouring blocks.
  A window's candidates start as soon as that window is rated, not after
  the slowest window.

The condition that a qualifying rule, limit or exception counts as part of
the answer, and that the query may hold several sub-questions or keywords,
is written into the question itself: Davi's native noul does not use
criteria (engineering_plan/05).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from ..decision import NoulQuestion
from . import tokens

PASS1 = (
    '`query` may hold several sub-questions or a list of keywords. Does '
    'passage {pid} in `passages` state something that answers at least one '
    'of them, such as a value, limit, date, rule, condition, exception or '
    'behaviour? A passage that only names a topic from `query` does not.'
)
PASS2 = (
    '`query` may hold several sub-questions or a list of keywords. Does '
    '`passage` state something that answers at least one of them, such as '
    'a value, limit, date, rule, condition, exception or behaviour? A '
    'passage that only names a topic from `query` does not. `before` and '
    '`after` are the neighbouring text on the page, shown only to make '
    'clear what `passage` refers to; judge `passage` itself.'
)
CRITERIA = {
    'true': 'It states part of the answer to a sub-question in `query`, or '
    'a condition, exception or scope that qualifies such an answer.',
    'false': 'It is about something else, or only names a topic from `query`.',
}
ITEM_OVERHEAD_TOKENS = 20  # the id and section sent with each passage


class RelevanceJudge:
    """``rate(blocks, query, candidate)`` -> (pass 1, pass 2) scores by id."""

    def __init__(
        self,
        client,
        *,
        window_tokens=3000,
        window_blocks=120,
        context_chars=400,
        concurrency=16,
    ):
        self._client = client
        self._window_tokens = window_tokens
        self._window_blocks = window_blocks
        self._context_chars = context_chars
        self._concurrency = concurrency
        self._requests = 0
        self._lock = threading.Lock()

    @property
    def requests(self):
        return self._requests

    def windows(self, blocks):
        """Consecutive blocks within the window limits (a single long block
        gets a window of its own)."""
        out, current, size = [], [], 0
        for block in blocks:
            cost = tokens.estimate(block.text) + ITEM_OVERHEAD_TOKENS
            if current and (
                size + cost > self._window_tokens
                or len(current) >= self._window_blocks
            ):
                out.append(current)
                current, size = [], 0
            current.append(block)
            size += cost
        if current:
            out.append(current)
        return out

    def rate(self, blocks, query, candidate):
        """Pass 1 for every block, pass 2 for the candidates of each window.
        A failed request raises ``DecisionError``."""
        position = {block.id: index for index, block in enumerate(blocks)}
        first, second = {}, {}
        with (
            ThreadPoolExecutor(self._concurrency) as windows,
            ThreadPoolExecutor(self._concurrency) as alone,
        ):

            def window(group):
                scores = self._window(group, query)
                picked = [i for i, p in scores.items() if p >= candidate]
                again = list(
                    alone.map(
                        lambda i: self._alone(blocks, position[i], query),
                        picked,
                    )
                )
                return scores, dict(zip(picked, again))

            # merged in page order, so ties are broken the same way each run
            for scores, rescored in windows.map(window, self.windows(blocks)):
                first.update(scores)
                second.update(rescored)
        return first, second

    # --- requests ------------------------------------------------------------
    def _count(self):
        with self._lock:
            self._requests += 1

    def _window(self, group, query):
        self._count()
        state = {
            'query': query,
            'passages': [
                {'id': f'p{b.id}', 'section': b.section, 'text': b.text}
                for b in group
            ],
        }
        questions = {
            f'p{b.id}': NoulQuestion(PASS1.format(pid=f'p{b.id}'), CRITERIA)
            for b in group
        }
        reply = self._client.ask(state, questions)
        return {int(key[1:]): a.p_true for key, a in reply.answers.items()}

    def _alone(self, blocks, index, query):
        self._count()
        block = blocks[index]
        before = blocks[index - 1].text if index > 0 else ''
        after = blocks[index + 1].text if index + 1 < len(blocks) else ''
        state = {
            'query': query,
            'section': block.section,
            'passage': block.text,
            'before': before[-self._context_chars :],
            'after': after[: self._context_chars],
        }
        return self._client.decide(state, NoulQuestion(PASS2, CRITERIA)).p_true
