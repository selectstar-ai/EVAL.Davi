# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""The ``davi-read`` command.

    davi-read URL "question"          read a web page
    davi-read --file PATH "question"  read a local HTML, Markdown or text file

The judge is Davi by default (``--judge jev`` for TypeSafe Jev). Options
come from ``DAVI_ROUTE_<KEY>``, ``~/.claude/davi-route.json`` (written by
``/davi-route:setup``) and ``CLAUDE_PLUGIN_OPTION_<KEY>``, as for the
routing hooks: ``davi_base_url``, ``noul_mode``, ``read_allow_hosts``
(comma-separated hosts that may resolve to private addresses).

Exit codes: 0 with a result (including no_match, empty and judge_failed,
which still print the page), 1 when the page could not be read, 2 for a
usage or configuration error. Nothing but the result goes to stdout; page
text, questions and keys are never logged.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path

from ..decision import build_client, with_options_file
from ..decision.settings import option
from .fetch import PageFetcher
from .reader import ReadConfig, Reader, Thresholds, format_result

MAX_FILE_BYTES = 5_000_000
DENIED_NAMES = ('.env*', '*.pem', '*.key', 'id_*')
FILE_TYPES = {
    '.html': 'text/html',
    '.htm': 'text/html',
    '.md': 'text/markdown',
    '.markdown': 'text/markdown',
    '.txt': 'text/plain',
    '.rst': 'text/plain',
}


class UsageError(Exception):
    """A bad argument or option; printed and exits 2."""


def read_local(path):
    """(text, media type) of a local file; refuses key-like files."""
    target = Path(path)
    if any(fnmatch.fnmatch(target.name, pattern) for pattern in DENIED_NAMES):
        raise UsageError('refusing to read a key or environment file')
    if '.git' in target.parts:
        raise UsageError('refusing to read inside .git')
    if not target.is_file():
        raise UsageError('no such file')
    if target.stat().st_size > MAX_FILE_BYTES:
        raise UsageError(f'the file is over {MAX_FILE_BYTES} bytes')
    data = target.read_bytes()
    if b'\x00' in data[:4096]:
        raise UsageError('not a text file')
    media = FILE_TYPES.get(target.suffix.lower(), 'text/plain')
    return data.decode('utf-8', errors='replace'), media


def _parser():
    parser = argparse.ArgumentParser(
        prog='davi-read',
        description='Print the parts of a page that answer a question, '
        'copied verbatim.',
    )
    parser.add_argument('target', help='a URL, or a path with --file')
    parser.add_argument('question')
    parser.add_argument('--file', action='store_true', help='read a file')
    parser.add_argument('--judge', choices=('davi', 'jev'), default=None)
    parser.add_argument('--noul-mode', choices=('choice', 'native'))
    parser.add_argument('--allow-host', action='append', default=[])
    parser.add_argument('--candidate', type=float)
    parser.add_argument('--keep', type=float)
    parser.add_argument('--neighbour', type=float)
    parser.add_argument('--json', action='store_true')
    return parser


def _thresholds(args):
    base = Thresholds()
    values = {
        'candidate': base.candidate
        if args.candidate is None
        else args.candidate,
        'keep': base.keep if args.keep is None else args.keep,
        'neighbour': base.neighbour
        if args.neighbour is None
        else args.neighbour,
    }
    for name, value in values.items():
        if not 0.0 <= value <= 1.0:
            raise UsageError(f'--{name} must be in [0, 1]')
    return Thresholds(**values)


def run(argv, env, transport=None, fetcher=None, out=None):
    """The command, with injectable environment and transports (tests)."""
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    env = with_options_file(env)
    try:
        judge = args.judge or option(env, 'read_judge', 'davi')
        client = build_client(
            judge, env, noul_mode=args.noul_mode, transport=transport
        )
        thresholds = _thresholds(args)
    except (ValueError, UsageError) as error:
        print(f'davi-read: {error}', file=sys.stderr)
        return 2
    allow = list(args.allow_host)
    allow += [
        host.strip()
        for host in (option(env, 'read_allow_hosts') or '').split(',')
        if host.strip()
    ]
    reader = Reader(
        client,
        ReadConfig(thresholds=thresholds),
        fetcher or PageFetcher(allow),
    )
    with client:
        if args.file:
            try:
                text, media = read_local(args.target)
            except UsageError as error:
                print(f'davi-read: {error}', file=sys.stderr)
                return 2
            result = reader.read_text(text, args.question, media)
            result.url = Path(args.target).name
        else:
            result = reader.read_url(args.target, args.question)
    if args.json:
        out.write(json.dumps(result.to_dict(), ensure_ascii=False) + '\n')
    else:
        out.write(format_result(result) + '\n')
    return 1 if result.status == 'fetch_error' else 0


def main(argv=None):
    sys.exit(run(sys.argv[1:] if argv is None else argv, dict(os.environ)))
