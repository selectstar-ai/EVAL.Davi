# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""The ``davi-decision-mcp`` command: a local stdio MCP server.

    davi-decision-mcp [--judge davi|jev] [--noul-mode choice|native]
                      [--timeout SECONDS] [--allowed-root DIR ...]

Each setting falls back to ``DAVI_ROUTE_<KEY>``, then
``CLAUDE_PLUGIN_OPTION_<KEY>``, then ``~/.claude/davi-route.json``:
``decision_judge`` (default davi), ``noul_mode`` (default choice),
``decision_timeout_seconds`` (default 60), ``decision_allowed_roots``
(``os.pathsep``-separated; default the working directory),
``davi_model`` / ``jev_model``, ``jev_base_url``. The Davi URL is
``DAVI_BASE_URL`` (or ``DAVI_ROUTE_DAVI_BASE_URL``); keys are
``DAVI_API_KEY`` and ``TYPESAFE_API_KEY`` / ``JEV_API_KEY``, read at the
first call.

stdout carries the MCP protocol only; messages go to stderr. Exit codes:
0 after a clean stop, 2 for a setting error or a missing MCP SDK.
"""

from __future__ import annotations

import argparse
import os
import sys

MIN_PYTHON = (3, 10)
SDK_RANGE = 'mcp>=2.3,<3'
INSTALL_HINT = (
    'install it with pip install "judge-adapter[mcp]" in a Python 3.10+ '
    'environment'
)


def _fail(message):
    print(f'davi-decision-mcp: {message}', file=sys.stderr)
    return 2


def sdk_problem(version_info=None, find_version=None):
    """Why the MCP SDK cannot run here, or None."""
    version_info = version_info or sys.version_info
    if tuple(version_info[:2]) < MIN_PYTHON:
        found = '.'.join(str(part) for part in version_info[:2])
        return (
            f'needs Python 3.10 or newer for the MCP SDK (this is {found}); '
            + INSTALL_HINT
        )
    if find_version is None:
        from importlib.metadata import PackageNotFoundError, version

        def find_version():
            try:
                return version('mcp')
            except PackageNotFoundError:
                return None

    found = find_version()
    if found is None:
        return 'the MCP SDK is not installed; ' + INSTALL_HINT
    major = found.split('.')[0]
    if major != '2':
        return f'needs {SDK_RANGE} (found mcp {found}); ' + INSTALL_HINT
    return None


def _parser():
    parser = argparse.ArgumentParser(
        prog='davi-decision-mcp',
        description='Local stdio MCP server with one tool, decide.',
    )
    parser.add_argument('--judge', choices=('davi', 'jev'), default=None)
    parser.add_argument(
        '--noul-mode', choices=('choice', 'native'), default=None
    )
    parser.add_argument('--timeout', type=float, default=None)
    parser.add_argument(
        '--allowed-root',
        action='append',
        default=None,
        metavar='DIR',
        help='a folder state_files may read from (repeatable; '
        'default: the working directory)',
    )
    parser.add_argument(
        '--log-level',
        choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'),
        default='WARNING',
    )
    return parser


def main(argv=None):
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    problem = sdk_problem()
    if problem:
        sys.exit(_fail(problem))
    from .server import build_server
    from .tool import Decider

    if args.timeout is not None and not args.timeout > 0:
        sys.exit(_fail('--timeout must be a positive number of seconds'))
    try:
        decider = Decider.from_env(
            dict(os.environ),
            judge=args.judge,
            noul_mode=args.noul_mode,
            timeout=args.timeout,
            roots=args.allowed_root,
        )
    except ValueError as error:
        sys.exit(_fail(str(error)))
    server = build_server(decider, log_level=args.log_level)
    try:
        server.run('stdio')
    except KeyboardInterrupt:
        pass
    finally:
        decider.close()


if __name__ == '__main__':
    main()
