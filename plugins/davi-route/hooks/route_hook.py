#!/usr/bin/env python3
# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Launcher for ``judge_adapter.routing.hooks`` (davi-route plugin).

Standard library only until the hook runs. Usage (hooks/hooks.json, exec
form)::

    python3 ${CLAUDE_PLUGIN_ROOT}/hooks/route_hook.py <pre|post|stop|...>

- Finds the repository's ``src``: ``DAVI_ROUTE_SRC`` if set, else by walking
  up from this file (``--plugin-dir plugins/davi-route`` in a checkout),
  else in the marketplace's copy of the repository. A marketplace install
  copies only ``plugins/davi-route`` to
  ``<plugins>/cache/<marketplace>/davi-route/<version>/``, while the
  marketplace itself is the whole repository (its ``installLocation``).
- Fast path: if the hook's feature is off (``route_enabled`` for pre/post,
  ``stop_verify_enabled`` for stop) and probe mode is off, it exits 0
  before reading stdin or importing anything.
- Needs httpx for real verifier calls: if ``python3`` cannot import it,
  ``${CLAUDE_PLUGIN_DATA}/venv`` is tried; otherwise it logs
  ``dependency_missing`` and exits 0 (fail-open). Never installs anything.
"""

import json
import os
import sys

KINDS = ('pre', 'post', 'stop', 'subagent_stop')
REEXEC_GUARD = '_DAVI_ROUTE_HOOK_REEXEC'
OPTION_PREFIXES = ('DAVI_ROUTE_', 'CLAUDE_PLUGIN_OPTION_')
TRUE_WORDS = ('1', 'true', 'yes', 'on')
OFF_WORDS = ('', '0', 'false', 'no', 'off')
VENV_PYTHONS = (('venv', 'bin', 'python'), ('venv', 'Scripts', 'python.exe'))


def _opt(key):
    for prefix in OPTION_PREFIXES:
        value = os.environ.get(prefix + key.upper(), '').strip().lower()
        if value:
            return value
    return ''


def feature_on(kind):
    if _opt('probe') in TRUE_WORDS:
        return True
    if kind in ('pre', 'post'):
        key = 'route_enabled'
    else:
        key = 'stop_verify_enabled'
    # an invalid value runs main(), which reports it
    return _opt(key) not in OFF_WORDS


def _has_routing(src):
    return os.path.isdir(os.path.join(src, 'judge_adapter', 'routing'))


def _install_location(plugins_dir, name):
    path = os.path.join(plugins_dir, 'known_marketplaces.json')
    try:
        with open(path, encoding='utf-8') as file:
            entry = json.load(file).get(name)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(entry, dict):
        return None
    location = entry.get('installLocation')
    return location if isinstance(location, str) else None


def _marketplace_src(here):
    """``src`` in the marketplace's copy of the repository.

    ``here`` is ``<plugins>/cache/<marketplace>/<plugin>/<version>/hooks``
    for an installed plugin. The marketplace lives at its
    ``installLocation`` in ``<plugins>/known_marketplaces.json`` (a local
    directory marketplace points at the checkout itself), by default
    ``<plugins>/marketplaces/<marketplace>``.
    """
    version_dir = os.path.dirname(here)
    market_dir = os.path.dirname(os.path.dirname(version_dir))
    cache_dir = os.path.dirname(market_dir)
    if os.path.basename(cache_dir) != 'cache':
        return None
    plugins_dir = os.path.dirname(cache_dir)
    name = os.path.basename(market_dir)
    roots = (
        _install_location(plugins_dir, name),
        os.path.join(plugins_dir, 'marketplaces', name),
    )
    for root in roots:
        if root and _has_routing(os.path.join(root, 'src')):
            return os.path.join(root, 'src')
    return None


def find_src():
    configured = os.environ.get('DAVI_ROUTE_SRC', '').strip()
    if configured and os.path.isdir(os.path.join(configured, 'judge_adapter')):
        return configured
    here = os.path.dirname(os.path.abspath(__file__))
    directory = here
    for _ in range(6):
        candidate = os.path.join(directory, 'src')
        if _has_routing(candidate):
            return candidate
        directory = os.path.dirname(directory)
    return _marketplace_src(here)


def _venv_python():
    data = os.environ.get('CLAUDE_PLUGIN_DATA')
    if not data:
        return None
    for parts in VENV_PYTHONS:
        path = os.path.join(data, *parts)
        if os.path.isfile(path):
            return path
    return None


def _note(kind, status, detail):
    record = {
        'event': 'davi-route',
        'kind': kind,
        'status': status,
        'detail': detail,
    }
    sys.stderr.write(json.dumps(record) + '\n')


def main(kind):
    import importlib.util

    if importlib.util.find_spec('httpx') is None:
        python = _venv_python()
        if python and not os.environ.get(REEXEC_GUARD):
            os.environ[REEXEC_GUARD] = '1'
            os.execv(python, [python, os.path.abspath(__file__), kind])
        _note(
            kind,
            'dependency_missing',
            'httpx is not importable by python3 '
            '(pip install "httpx>=0.24,<1")',
        )
        return 0
    src = find_src()
    if src is None:
        _note(
            kind,
            'src_missing',
            'could not find src/judge_adapter/routing (checkout or '
            'marketplace clone); set DAVI_ROUTE_SRC',
        )
        return 0
    sys.path.insert(0, src)
    from judge_adapter.routing.hooks import main as hook_main

    return hook_main(kind)


if __name__ == '__main__':
    hook_kind = sys.argv[1] if len(sys.argv) > 1 else ''
    if hook_kind not in KINDS or not feature_on(hook_kind):
        sys.exit(0)
    try:
        main(hook_kind)
    except Exception as error:  # noqa: BLE001 - fail-open
        _note(hook_kind, 'launcher_error', type(error).__name__)
    sys.exit(0)
