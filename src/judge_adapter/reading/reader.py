# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""``davi-read``: the parts of a page that answer a question, verbatim.

    Reader(client).read_url(url, query) -> ReadResult
    Reader(client).read_text(text, query, media_type) -> ReadResult

Steps: fetch, split into blocks, return a short page whole, otherwise rate
every block (pass 1), rate the candidates again alone (pass 2), keep the
blocks at or above the keep threshold plus their close neighbours, the
first block and the header rows of kept table rows, then top up a thin
result by score.

Fail-open: when the judge fails, the page goes out unjudged (cut at
``failopen_chars``) with status ``judge_failed``; the agent can still read
it. Thresholds are settings, not facts: the defaults are a starting point
until they are fitted on labelled pages for each judge model.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from ..decision import DecisionError
from .blocks import parser_for, render
from .fetch import FetchError, PageFetcher
from .judge import RelevanceJudge

STATUSES = (
    'filtered',
    'passthrough',
    'no_match',
    'empty',
    'judge_failed',
    'fetch_error',
)


@dataclass(frozen=True)
class Thresholds:
    candidate: float = 0.6
    keep: float = 0.8
    neighbour: float = 0.6


@dataclass(frozen=True)
class ReadConfig:
    thresholds: Thresholds = field(default_factory=Thresholds)
    passthrough_chars: int = 6000
    min_output_chars: int = 900
    failopen_chars: int = 40000
    window_chars: int = 9000
    window_blocks: int = 60
    context_chars: int = 400
    concurrency: int = 8


@dataclass
class ReadResult:
    status: str
    text: str
    url: str = ''
    blocks_total: int = 0
    blocks_kept: int = 0
    page_chars: int = 0
    out_chars: int = 0
    judge_requests: int = 0
    judge_seconds: float = 0.0
    provider: str = ''
    noul_mode: str = ''
    error: Optional[str] = None

    def to_dict(self):
        return asdict(self)


def choose(blocks, first, second, thresholds, min_output_chars):
    """Block ids to keep. ``first``/``second`` are pass scores by id."""
    by_id = {block.id: block for block in blocks}
    keep = {i for i, p in second.items() if p >= thresholds.keep}

    def score(i):
        return second.get(i, first.get(i, 0.0))

    for i in list(keep):
        for j in (i - 1, i + 1):
            near = by_id.get(j)
            if (
                near is not None
                and near.path == by_id[i].path
                and score(j) >= thresholds.neighbour
            ):
                keep.add(j)
    if blocks:
        keep.add(blocks[0].id)
    ranked = sorted(second, key=lambda i: (-second[i], i)) + sorted(
        (i for i in first if i not in second), key=lambda i: (-first[i], i)
    )
    body = sum(len(by_id[i].text) for i in keep if i != blocks[0].id)
    for i in ranked:
        if body >= min_output_chars:
            break
        if i not in keep:
            keep.add(i)
            body += len(by_id[i].text)
    tables = {by_id[i].table for i in keep if by_id[i].table}
    keep |= {b.id for b in blocks if b.head and b.table in tables}
    return keep


class Reader:
    """Fetches and filters pages with one decision client."""

    def __init__(self, client, config=None, fetcher=None, clock=None):
        self._client = client
        self._config = config or ReadConfig()
        self._fetcher = fetcher or PageFetcher()
        self._clock = clock or time.perf_counter

    def read_url(self, url, query):
        try:
            page = self._fetcher.fetch(url)
        except FetchError as error:
            return self._result('fetch_error', '', url=url, error=str(error))
        result = self.read_text(
            page.text, query, page.media_type, page.final_url
        )
        result.url = url
        return result

    def read_text(self, text, query, media_type='text/html', base_url=''):
        blocks = parser_for(media_type).parse(text, base_url)
        whole = render(blocks)
        if not blocks:
            return self._result('empty', '', url=base_url)
        common = {
            'url': base_url,
            'blocks_total': len(blocks),
            'page_chars': len(whole),
        }
        if len(whole) <= self._config.passthrough_chars:
            return self._result(
                'passthrough', whole, blocks_kept=len(blocks), **common
            )
        judge = RelevanceJudge(
            self._client,
            window_chars=self._config.window_chars,
            window_blocks=self._config.window_blocks,
            context_chars=self._config.context_chars,
            concurrency=self._config.concurrency,
        )
        thresholds = self._config.thresholds
        started = self._clock()
        try:
            first = judge.first_pass(blocks, query)
            candidates = sorted(
                i for i, p in first.items() if p >= thresholds.candidate
            )
            second = judge.second_pass(blocks, candidates, query)
        except DecisionError as error:
            cut = whole[: self._config.failopen_chars]
            return self._result(
                'judge_failed',
                cut,
                blocks_kept=len(blocks),
                judge_requests=judge.requests,
                judge_seconds=self._clock() - started,
                error=error.code,
                **common,
            )
        keep = choose(
            blocks, first, second, thresholds, self._config.min_output_chars
        )
        status = (
            'filtered'
            if any(p >= thresholds.keep for p in second.values())
            else 'no_match'
        )
        return self._result(
            status,
            render(blocks, keep),
            blocks_kept=len(keep),
            judge_requests=judge.requests,
            judge_seconds=self._clock() - started,
            **common,
        )

    def _result(self, status, text, **fields):
        result = ReadResult(status, text, **fields)
        result.out_chars = len(text)
        result.judge_seconds = round(result.judge_seconds, 3)
        result.provider = getattr(self._client, 'provider', '')
        result.noul_mode = getattr(self._client, 'noul_mode', '')
        return result


HEADLINES = {
    'filtered': "blocks are copied from the page; '##' lines are its "
    'heading path',
    'passthrough': 'the page is short and returned whole',
    'no_match': 'no block passed the threshold; the closest blocks follow. '
    'The answer may still be on the page',
    'empty': 'no text could be read from the page',
    'judge_failed': 'the judge failed, so the page follows unjudged',
    'fetch_error': 'the page could not be read',
}


def format_result(result):
    """The text an agent reads: '#' header lines, then the blocks."""
    lines = [
        f'# davi-read: {result.url}',
        f'# status: {result.status} ({HEADLINES[result.status]})',
    ]
    if result.status == 'fetch_error':
        lines.append(f'# error: {result.error}')
        return '\n'.join(lines)
    if result.status == 'judge_failed':
        lines.append(f'# error: {result.error}')
        if result.out_chars < result.page_chars:
            lines.append(
                f'# cut at {result.out_chars:,} of '
                f'{result.page_chars:,} characters'
            )
    elif result.status in ('filtered', 'no_match'):
        lines.append(
            f'# kept {result.blocks_kept}/{result.blocks_total} blocks, '
            f'{result.out_chars:,} of {result.page_chars:,} characters; '
            f'judge {result.provider} ({result.noul_mode}), '
            f'{result.judge_requests} requests, {result.judge_seconds:.1f} s'
        )
    if result.text:
        lines.append(result.text)
    return '\n'.join(lines)
