# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""``davi-read``: the parts of a page that answer a question, verbatim.

    Reader(client).read_url(url, query) -> ReadResult
    Reader(client).read_text(text, query, media_type) -> ReadResult

The selection follows jevfetch's method, so that the judge model is the only
difference between the two:

1. A page of at most ``passthrough_tokens`` is returned whole.
2. Every block is rated in windows (pass 1); blocks at or above
   ``candidate`` are rated again alone with their neighbours (pass 2).
3. Kept: pass-2 scores at or above ``keep``; a block right before or after
   a kept one under the same heading from ``neighbour`` (its pass-2 score);
   the first block; the header row of a kept table row.
4. A result under ``min_output_tokens`` (first block excluded) is topped up
   by pass-2, then pass-1 score.

Added for davi-read: the judge is Davi by default (internal pages stay in
house); when the judge fails the page goes out unjudged (``judge_failed``,
cut at ``failopen_chars``); fetching refuses private addresses. Thresholds
are jevfetch's values, a starting point until they are fitted per model.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from ..decision import DecisionError
from . import tokens
from .blocks import parser_for, render
from .fetch import (
    ChromeRenderer,
    FetchError,
    PageFetcher,
    default_files_dir,
)
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
    passthrough_tokens: int = 2000
    min_output_tokens: int = 300
    window_tokens: int = 3000
    window_blocks: int = 120
    context_chars: int = 400
    concurrency: int = 16
    render_below_tokens: int = 200
    failopen_chars: int = 40000


@dataclass
class ReadResult:
    status: str
    text: str
    url: str = ''
    blocks_total: int = 0
    blocks_kept: int = 0
    page_tokens: int = 0
    out_tokens: int = 0
    judge_requests: int = 0
    judge_seconds: float = 0.0
    provider: str = ''
    noul_mode: str = ''
    rendered: bool = False
    saved: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self):
        return asdict(self)


def choose(blocks, first, second, thresholds, min_output_tokens):
    """Block ids to keep. ``first``/``second`` are pass scores by id."""
    by_id = {block.id: block for block in blocks}
    keep = {i for i, p in second.items() if p >= thresholds.keep}
    for i in list(keep):
        for j in (i - 1, i + 1):
            near = by_id.get(j)
            if (
                near is not None
                and near.path == by_id[i].path
                and second.get(j, 0.0) >= thresholds.neighbour
            ):
                keep.add(j)
    head = blocks[0].id
    keep.add(head)
    ranked = sorted(second, key=lambda i: (-second[i], i)) + sorted(
        (i for i in first if i not in second), key=lambda i: (-first[i], i)
    )
    body = sum(tokens.estimate(by_id[i].text) for i in keep if i != head)
    for i in ranked:
        if body >= min_output_tokens:
            break
        if i not in keep:
            keep.add(i)
            body += tokens.estimate(by_id[i].text)
    tables = {by_id[i].table for i in keep if by_id[i].table}
    keep |= {b.id for b in blocks if b.head and b.table in tables}
    return keep


class Reader:
    """Fetches and filters pages with one decision client."""

    def __init__(
        self, client, config=None, fetcher=None, renderer=None, clock=None
    ):
        self._client = client
        self._config = config or ReadConfig()
        self._fetcher = fetcher or PageFetcher()
        self._renderer = renderer
        self._clock = clock or time.perf_counter

    def read_url(self, url, query):
        try:
            page = self._fetcher.fetch(url)
        except FetchError as error:
            result = self._result('fetch_error', '', url=url, error=str(error))
            result.saved = error.saved
            return result
        text, media, rendered = page.text, page.media_type, False
        if self._renderer is not None and media == 'text/html':
            text, rendered = self._render_if_empty(page)
        result = self.read_text(text, query, media, page.final_url)
        result.url, result.rendered = url, rendered
        return result

    def read_text(self, text, query, media_type='text/html', base_url=''):
        blocks = parser_for(media_type).parse(text, base_url)
        whole = render(blocks)
        if not blocks:
            return self._result('empty', '', url=base_url)
        common = {
            'url': base_url,
            'blocks_total': len(blocks),
            'page_tokens': tokens.estimate(whole),
        }
        if common['page_tokens'] <= self._config.passthrough_tokens:
            return self._result(
                'passthrough', whole, blocks_kept=len(blocks), **common
            )
        judge = RelevanceJudge(
            self._client,
            window_tokens=self._config.window_tokens,
            window_blocks=self._config.window_blocks,
            context_chars=self._config.context_chars,
            concurrency=self._config.concurrency,
        )
        thresholds = self._config.thresholds
        started = self._clock()
        try:
            first, second = judge.rate(blocks, query, thresholds.candidate)
        except DecisionError as error:
            return self._result(
                'judge_failed',
                whole[: self._config.failopen_chars],
                blocks_kept=len(blocks),
                judge_requests=judge.requests,
                judge_seconds=self._clock() - started,
                error=error.code,
                **common,
            )
        keep = choose(
            blocks, first, second, thresholds, self._config.min_output_tokens
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

    def _render_if_empty(self, page):
        """Re-read with the browser when the static HTML has almost no text
        and the rendered DOM has more."""
        parser = parser_for(page.media_type)
        size = tokens.estimate(render(parser.parse(page.text)))
        if size >= self._config.render_below_tokens:
            return page.text, False
        dom = self._renderer.render(page.final_url)
        if dom and tokens.estimate(render(parser.parse(dom))) > size:
            return dom, True
        return page.text, False

    def _result(self, status, text, **fields):
        result = ReadResult(status, text, **fields)
        result.out_tokens = tokens.estimate(text)
        result.judge_seconds = round(result.judge_seconds, 3)
        result.provider = getattr(self._client, 'provider', '')
        result.noul_mode = getattr(self._client, 'noul_mode', '')
        return result


def default_reader(client, config=None, allow_hosts=(), render_pages=True):
    """A reader with the standard fetcher (refused files saved to the
    temporary folder) and, if asked, the headless Chrome renderer."""
    return Reader(
        client,
        config,
        PageFetcher(allow_hosts, files_dir=default_files_dir()),
        ChromeRenderer() if render_pages else None,
    )


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
        if result.saved:
            lines.append(f'# saved: {result.saved}')
        return '\n'.join(lines)
    if result.rendered:
        lines.append('# rendered with a headless browser')
    if result.status == 'judge_failed':
        lines.append(f'# error: {result.error}')
        if result.out_tokens < result.page_tokens:
            lines.append(
                f'# cut at about {result.out_tokens:,} of '
                f'{result.page_tokens:,} tokens'
            )
    elif result.status in ('filtered', 'no_match'):
        lines.append(
            f'# kept {result.blocks_kept}/{result.blocks_total} blocks, '
            f'about {result.out_tokens:,} of {result.page_tokens:,} tokens; '
            f'judge {result.provider} ({result.noul_mode}), '
            f'{result.judge_requests} requests, {result.judge_seconds:.1f} s'
        )
    if result.text:
        lines.append(result.text)
    return '\n'.join(lines)
