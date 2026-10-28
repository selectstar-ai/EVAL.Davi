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

A request that fails with a timeout or a server error is tried again (up to
``retries`` times, waiting ``backoff`` x 1, 2, 4 ... seconds). If a window
still fails, only its blocks are reported as unjudged and the rest of the
page is judged as usual; a candidate whose pass-2 request fails is reported
unjudged too, so the reader keeps it rather than lose it. Only when every
window fails, or the key is missing, does ``rate`` raise.

The condition that a qualifying rule, limit or exception counts as part of
the answer, and that the query may hold several sub-questions or keywords,
is written into the question itself: Davi's native noul does not use
criteria (engineering_plan/05).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..decision import DecisionError, NoulQuestion
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
RETRY_CODES = frozenset({'timeout', 'provider_error'})


@dataclass(frozen=True)
class Rating:
    """Pass-1 and pass-2 scores by block id, and the ids left unjudged."""

    first: dict
    second: dict
    unjudged: frozenset = frozenset()


class RelevanceJudge:
    """``rate(blocks, query, candidate)`` -> ``Rating``."""

    def __init__(
        self,
        client,
        *,
        window_tokens=3000,
        window_blocks=120,
        context_chars=400,
        concurrency=16,
        retries=3,
        backoff=0.5,
        sleep=time.sleep,
    ):
        self._client = client
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep
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
        Raises ``DecisionError`` only when every window fails or the key is
        missing."""
        position = {block.id: index for index, block in enumerate(blocks)}
        groups = self.windows(blocks)
        first, second, unjudged, errors = {}, {}, set(), []
        with ThreadPoolExecutor(self._concurrency) as windows:
            with ThreadPoolExecutor(self._concurrency) as alone:

                def window(group):
                    try:
                        scores = self._retry(self._window, group, query)
                    except DecisionError as error:
                        if error.code == 'credential_missing':
                            raise
                        return {}, {}, {b.id for b in group}, error
                    picked = [i for i, p in scores.items() if p >= candidate]
                    again = list(
                        alone.map(
                            lambda i: self._alone_or_none(
                                blocks, position[i], query
                            ),
                            picked,
                        )
                    )
                    rescored = {
                        i: p for i, p in zip(picked, again) if p is not None
                    }
                    failed = {i for i, p in zip(picked, again) if p is None}
                    return scores, rescored, failed, None

                # merged in page order, so ties break the same way each run
                for scores, rescored, failed, error in windows.map(
                    window, groups
                ):
                    first.update(scores)
                    second.update(rescored)
                    unjudged |= failed
                    if error is not None:
                        errors.append(error)
        if groups and len(errors) == len(groups):
            raise errors[-1]
        return Rating(first, second, frozenset(unjudged))

    def _retry(self, call, *args):
        for attempt in range(self._retries + 1):
            try:
                return call(*args)
            except DecisionError as error:
                if error.code not in RETRY_CODES or attempt == self._retries:
                    raise
                self._sleep(self._backoff * 2**attempt)

    def _alone_or_none(self, blocks, index, query):
        try:
            return self._retry(self._alone, blocks, index, query)
        except DecisionError as error:
            if error.code == 'credential_missing':
                raise
            return None

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
