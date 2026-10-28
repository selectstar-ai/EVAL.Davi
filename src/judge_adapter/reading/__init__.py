# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""``davi-read``: the parts of a page that answer a question, verbatim,
chosen by a decision model (Davi by default) through the decision core."""

from .blocks import (
    Block,
    BlockParser,
    HtmlBlockParser,
    TextBlockParser,
    render,
)
from .fetch import ChromeRenderer, FetchError, Page, PageFetcher
from .judge import RelevanceJudge
from .reader import (
    STATUSES,
    ReadConfig,
    Reader,
    ReadResult,
    Thresholds,
    choose,
    default_reader,
    format_result,
)

__all__ = [
    'STATUSES',
    'Block',
    'BlockParser',
    'ChromeRenderer',
    'FetchError',
    'HtmlBlockParser',
    'Page',
    'PageFetcher',
    'ReadConfig',
    'ReadResult',
    'Reader',
    'RelevanceJudge',
    'TextBlockParser',
    'Thresholds',
    'choose',
    'default_reader',
    'format_result',
    'render',
]
