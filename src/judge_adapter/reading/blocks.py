# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Split a page into blocks that each carry their heading path.

    BlockParser (abstract) .parse(text, base_url) -> [Block]
    ├── HtmlBlockParser       paragraphs, list items, code, table rows,
    │                         definition terms joined to the heading path;
    │                         navigation, footers and scripts dropped
    └── TextBlockParser       Markdown or plain text

A block's text is the page's own text: whitespace is collapsed (except in
code), links are written ``[text](url)`` and table cells are joined with
`` | ``. Nothing else is changed, so a kept block can be quoted.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import urljoin

MAX_BLOCK_CHARS = 4000
MIN_MAIN_CHARS = 200

_SPACE_RE = re.compile(r'\s+')
_SENTENCE_END_RE = re.compile(r'(?<=[.!?。])\s+')
_MD_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
_MD_ITEM_RE = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+(.*)$')
_MD_TABLE_RULE_RE = re.compile(r'^\s*\|?\s*:?-{2,}')

SKIP_TAGS = frozenset(
    {
        'script',
        'style',
        'noscript',
        'template',
        'svg',
        'nav',
        'footer',
        'form',
        'button',
        'select',
        'iframe',
        'aside',
        'head',
    }
)
SKIP_ROLES = frozenset({'navigation', 'banner', 'contentinfo', 'search'})
ANCHOR_MARKS = frozenset({'¶', '#', '§', '🔗'})
MAIN_TAGS = frozenset({'main', 'article'})
HEADINGS = {f'h{level}': level for level in range(1, 7)}
FLUSH_TAGS = frozenset(
    {
        'p',
        'div',
        'section',
        'blockquote',
        'figcaption',
        'caption',
        'ul',
        'ol',
        'dl',
        'table',
        'details',
        'summary',
        'main',
        'article',
        'header',
        'hr',
    }
)
VOID_TAGS = frozenset(
    {
        'br',
        'hr',
        'img',
        'input',
        'meta',
        'link',
        'wbr',
        'source',
        'area',
        'col',
        'embed',
        'param',
        'track',
    }
)


@dataclass(frozen=True)
class Block:
    """One quotable piece of a page."""

    id: int
    text: str
    path: tuple = ()
    kind: str = 'text'  # text | item | code | row
    table: int = 0  # table number; 0 when not in a table
    head: bool = False  # a table header row
    main: bool = False  # inside <main> or <article>

    @property
    def section(self):
        return ' > '.join(self.path)


def collapse(text):
    return _SPACE_RE.sub(' ', text).strip()


def _pieces(text, kind):
    """Split an over-long block at line ends (code) or sentence ends."""
    if len(text) <= MAX_BLOCK_CHARS:
        return [text]
    units = (
        text.split('\n') if kind == 'code' else _SENTENCE_END_RE.split(text)
    )
    joiner = '\n' if kind == 'code' else ' '
    pieces, current = [], ''
    for unit in units:
        while len(unit) > MAX_BLOCK_CHARS:
            if current:
                pieces.append(current)
                current = ''
            pieces.append(unit[:MAX_BLOCK_CHARS])
            unit = unit[MAX_BLOCK_CHARS:]
        candidate = f'{current}{joiner}{unit}' if current else unit
        if len(candidate) > MAX_BLOCK_CHARS:
            pieces.append(current)
            current = unit
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


class BlockParser(ABC):
    """Text in, numbered blocks out."""

    def parse(self, text, base_url=''):
        raw = self._raw_blocks(text or '', base_url)
        blocks = []
        for block in raw:
            for piece in _pieces(block.text, block.kind):
                if piece.strip():
                    blocks.append(replace(block, id=len(blocks), text=piece))
        return blocks

    @abstractmethod
    def _raw_blocks(self, text, base_url):
        """Blocks before numbering and splitting."""


class _HtmlCollector(HTMLParser):
    """Walks the HTML once; see ``HtmlBlockParser``."""

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._stack = []  # (tag, skipping, main) per open element
        self._skip = 0
        self._main = 0
        self._title = None
        self._in_title = False
        self._title_parts = []
        self._headings = {}  # level -> text
        self._heading = None  # (level, [parts]) while inside h1..h6
        self._term = None
        self._term_parts = None
        self._buffer = []
        self._kind = 'text'
        self._pre = 0
        self._link = None  # (href, [parts])
        self._tables = 0
        self._table = 0
        self._row = None  # list of cell texts
        self._row_tags = []
        self._cell = None
        self._in_thead = False
        self.blocks = []

    # --- helpers -----------------------------------------------------------
    def _path(self):
        path = [self._headings[level] for level in sorted(self._headings)]
        if not path and self._title:
            path = [self._title]
        if self._term:
            path.append(self._term)
        return tuple(path)

    def _emit(self, text, kind, head=False):
        text = text.strip('\n') if kind == 'code' else collapse(text)
        if text:
            self.blocks.append(
                Block(
                    0,
                    text,
                    self._path(),
                    kind,
                    self._table if kind == 'row' else 0,
                    head,
                    self._main > 0,
                )
            )

    def _flush(self):
        if self._buffer:
            text = ''.join(self._buffer)
            kind = self._kind
            self._buffer = []
            if kind == 'item':
                text = '- ' + collapse(text)
            self._emit(text, kind)
        self._kind = 'text'

    def _write(self, text):
        if self._link is not None:
            self._link[1].append(text)
        elif self._cell is not None:
            self._cell.append(text)
        elif self._heading is not None:
            self._heading[1].append(text)
        elif self._term_parts is not None:
            self._term_parts.append(text)
        else:
            self._buffer.append(text)

    # --- HTMLParser hooks --------------------------------------------------
    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        skipping = (
            tag in SKIP_TAGS
            or attributes.get('role') in SKIP_ROLES
            or attributes.get('aria-hidden') == 'true'
            or 'hidden' in attributes
            or (tag == 'header' and not self._main)
        )
        main = tag in MAIN_TAGS or attributes.get('role') == 'main'
        if tag not in VOID_TAGS:
            self._stack.append((tag, skipping, main))
            self._skip += skipping
            self._main += main
        if tag == 'title':
            self._in_title = True
            self._title_parts = []
            return
        if self._skip:
            return
        if tag == 'br':
            self._write('\n' if self._pre else ' ')
        elif tag in HEADINGS:
            self._flush()
            self._heading = (HEADINGS[tag], [])
        elif tag == 'pre':
            self._flush()
            self._pre += 1
            self._kind = 'code'
        elif tag == 'li':
            self._flush()
            self._kind = 'item'
        elif tag == 'dt':
            self._flush()
            self._term_parts = []
        elif tag == 'dd':
            self._flush()
        elif tag == 'thead':
            self._in_thead = True
        elif tag == 'table':
            self._flush()
            self._tables += 1
            self._table = self._tables
        elif tag == 'tr':
            self._row, self._row_tags = [], []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []
            self._row_tags.append(tag)
        elif tag == 'a':
            href = attributes.get('href') or ''
            self._link = (href, [])
        elif tag in FLUSH_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if tag == 'title' and self._in_title:
            self._in_title = False
            self._title = collapse(''.join(self._title_parts)) or None
        if not any(open_tag == tag for open_tag, _, _ in self._stack):
            return  # a stray end tag
        # close up to the matching tag (HTML is often unbalanced)
        while self._stack:
            open_tag, skipping, main = self._stack.pop()
            self._skip -= skipping
            self._main -= main
            if not self._skip and not skipping:
                self._close(open_tag)
            if open_tag == tag:
                break

    def _close(self, tag):
        if tag in HEADINGS and self._heading is not None:
            level, parts = self._heading
            self._heading = None
            text = collapse(''.join(parts))
            self._headings = {
                key: value
                for key, value in self._headings.items()
                if key < level
            }
            if text:
                self._headings[level] = text
            self._term = None
        elif tag == 'a' and self._link is not None:
            href, parts = self._link
            self._link = None
            text = collapse(''.join(parts))
            if text in ANCHOR_MARKS and href.startswith('#'):
                return  # a heading's permalink mark, not page text
            if text and href and not href.startswith(('#', 'javascript:')):
                text = f'[{text}]({urljoin(self._base, href)})'
            if text:
                self._write(text)
        elif tag == 'pre':
            self._flush()
            self._pre = max(0, self._pre - 1)
        elif tag == 'dt' and self._term_parts is not None:
            self._term = collapse(''.join(self._term_parts)) or None
            self._term_parts = None
        elif tag == 'dl':
            self._flush()
            self._term = None
        elif tag in ('td', 'th') and self._cell is not None:
            self._row.append(collapse(''.join(self._cell)))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            cells = [cell for cell in self._row if cell]
            head = self._in_thead or (
                bool(self._row_tags)
                and all(cell_tag == 'th' for cell_tag in self._row_tags)
            )
            if cells:
                self._emit(' | '.join(cells), 'row', head)
            self._row = None
        elif tag == 'thead':
            self._in_thead = False
        elif tag == 'table':
            self._table = 0
        elif tag in ('li', 'dd') or tag in FLUSH_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._skip:
            return
        self._write(data)

    def close(self):
        super().close()
        self._flush()


class HtmlBlockParser(BlockParser):
    """HTML pages. Content inside ``<main>``/``<article>`` is preferred when
    it holds enough text; navigation and other page chrome is dropped."""

    def _raw_blocks(self, text, base_url):
        collector = _HtmlCollector(base_url)
        collector.feed(text)
        collector.close()
        blocks = collector.blocks
        main = [block for block in blocks if block.main]
        if sum(len(block.text) for block in main) >= MIN_MAIN_CHARS:
            return main
        return blocks


class TextBlockParser(BlockParser):
    """Markdown or plain text: ``#`` headings, list items, fenced code,
    pipe tables and blank-line paragraphs."""

    def _raw_blocks(self, text, base_url):
        blocks = []
        headings = {}
        paragraph = []
        fence = None
        code = []
        table = 0
        in_table = False
        table_rows = 0

        def path():
            return tuple(headings[level] for level in sorted(headings))

        def flush():
            if paragraph:
                blocks.append(Block(0, collapse(' '.join(paragraph)), path()))
                paragraph.clear()

        for line in text.splitlines():
            stripped = line.strip()
            if fence is not None:
                if stripped.startswith(fence):
                    blocks.append(Block(0, '\n'.join(code), path(), 'code'))
                    code.clear()
                    fence = None
                else:
                    code.append(line)
                continue
            if stripped.startswith(('```', '~~~')):
                flush()
                fence = stripped[:3]
                continue
            heading = _MD_HEADING_RE.match(stripped)
            if heading:
                flush()
                level = len(heading.group(1))
                headings = {k: v for k, v in headings.items() if k < level}
                headings[level] = collapse(heading.group(2))
                in_table = False
                continue
            if stripped.startswith('|') and stripped.endswith('|'):
                flush()
                if not in_table:
                    table += 1
                    in_table, table_rows = True, 0
                if _MD_TABLE_RULE_RE.match(stripped):
                    continue
                cells = [collapse(c) for c in stripped.strip('|').split('|')]
                blocks.append(
                    Block(
                        0,
                        ' | '.join(c for c in cells if c),
                        path(),
                        'row',
                        table,
                        table_rows == 0,
                    )
                )
                table_rows += 1
                continue
            in_table = False
            item = _MD_ITEM_RE.match(line)
            if item:
                flush()
                blocks.append(
                    Block(0, '- ' + collapse(item.group(1)), path(), 'item')
                )
                continue
            if not stripped:
                flush()
                continue
            paragraph.append(stripped)
        flush()
        if fence is not None and code:
            blocks.append(Block(0, '\n'.join(code), path(), 'code'))
        return blocks


def parser_for(media_type):
    """The parser for a media type ('' or unknown -> HTML)."""
    if media_type in ('text/plain', 'text/markdown', 'text/x-markdown'):
        return TextBlockParser()
    return HtmlBlockParser()


def render(blocks, keep=None):
    """Kept blocks in page order; a ``## path`` line whenever it changes."""
    lines = []
    last_path = None
    for block in blocks:
        if keep is not None and block.id not in keep:
            continue
        if block.path and block.path != last_path:
            lines.append('## ' + block.section)
        last_path = block.path
        lines.append(block.text)
    return '\n'.join(lines)
