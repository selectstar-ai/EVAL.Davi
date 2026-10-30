#!/usr/bin/env python3
# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Launcher for the ``davi-read`` command (davi-route plugin).

Standard library only until the command runs. The ``davi-read`` skill runs
it::

    python3 ${CLAUDE_PLUGIN_ROOT}/hooks/davi_read.py \
        --dialog-url "${user_config.davi_base_url}" URL "question"
    python3 ${CLAUDE_PLUGIN_ROOT}/hooks/davi_read.py \
        --dialog-url "${user_config.davi_base_url}" --file PATH "question"

- Finds the ``judge_adapter`` code as the hook launcher does
  (``route_hook.find_src``), in a folder that has the ``reading`` package.
- Options are the plugin's: ``DAVI_ROUTE_<KEY>``, then the settings
  dialog, then ``~/.claude/davi-route.json`` (``/davi-route:setup``). Only
  ``davi_base_url`` is needed. Commands run through the Bash tool get no
  ``CLAUDE_PLUGIN_OPTION_<KEY>``, so the skill passes the dialog's URL with
  a leading ``--dialog-url`` (keys from the dialog do not reach it).
- Needs httpx, from ``python3`` or the plugin's ``pydeps`` folder. Commands
  run through the Bash tool get no ``CLAUDE_PLUGIN_DATA``, so for an
  installed plugin the folder is derived as Claude Code names it:
  ``<plugins>/data/<plugin>-<marketplace>``. When httpx is missing and
  ``auto_install_deps`` is not off, the first call installs it there, as
  the SessionStart ``setup`` does for the hooks.

Exit codes are the command's (0 result, 1 page not read, 2 usage or
configuration), and 2 when the code or httpx cannot be found.
"""

import importlib
import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import route_hook  # noqa: E402 - the hook launcher's lookup and installer

UNSAFE_ID = re.compile(r'[^A-Za-z0-9_-]')


def plugin_data(here):
    """``CLAUDE_PLUGIN_DATA``, or the data folder of an installed plugin
    (``<plugins>/cache/<market>/<plugin>/<version>/hooks`` ->
    ``<plugins>/data/<plugin>-<market>``); None otherwise."""
    configured = os.environ.get('CLAUDE_PLUGIN_DATA', '').strip()
    if configured:
        return configured
    plugin_dir = os.path.dirname(os.path.dirname(here))
    market_dir = os.path.dirname(plugin_dir)
    cache_dir = os.path.dirname(market_dir)
    if os.path.basename(cache_dir) != 'cache':
        return None
    name = os.path.basename(plugin_dir) + '@' + os.path.basename(market_dir)
    return os.path.join(
        os.path.dirname(cache_dir), 'data', UNSAFE_ID.sub('-', name)
    )


def ensure_httpx(install=route_hook.install_dependency):
    """True when httpx is importable, installing it once if allowed."""
    route_hook.add_deps_path()
    if importlib.util.find_spec('httpx') is not None:
        return True
    deps = route_hook.deps_dir()
    if deps is None or not route_hook.install_allowed():
        return False
    sys.stderr.write('davi-read: installing httpx (first use)\n')
    status, detail = install(deps)
    if status != 'installed':
        sys.stderr.write(f'davi-read: {status}: {detail}\n')
        return False
    route_hook.add_deps_path()
    importlib.invalidate_caches()
    return importlib.util.find_spec('httpx') is not None


def take_dialog_url(argv):
    """``argv`` without a leading ``--dialog-url VALUE``; a set VALUE becomes
    the dialog's ``davi_base_url``, as the hooks receive it."""
    if argv[:1] != ['--dialog-url'] or len(argv) < 2:
        return argv
    url = route_hook.dialog_value(argv[1])
    if url:
        os.environ[route_hook.DIALOG_PREFIX + 'DAVI_BASE_URL'] = url
    return argv[2:]


def _fail(message):
    sys.stderr.write(f'davi-read: {message}\n')
    return 2


def main(argv, install=route_hook.install_dependency):
    here = os.path.dirname(os.path.abspath(__file__))
    route_hook.drop_stale_options()
    argv = take_dialog_url(argv)
    route_hook.load_config_file()
    data = plugin_data(here)
    if data:
        os.environ['CLAUDE_PLUGIN_DATA'] = data
    src = route_hook.find_src('reading')
    if src is None:
        return _fail(
            'could not find judge_adapter with the reading package (plugin '
            'lib, checkout or marketplace clone); set DAVI_ROUTE_SRC'
        )
    if not ensure_httpx(install):
        return _fail(
            'httpx is missing: pip install "httpx>=0.24,<1" for python3, '
            'or turn auto_install_deps on'
        )
    sys.path.insert(0, src)
    from judge_adapter.reading.cli import run

    return run(argv, dict(os.environ))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
