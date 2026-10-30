#!/usr/bin/env python3
# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Launcher for ``judge_adapter.routing.hooks`` (davi-route plugin).

Standard library only until the hook runs. Usage (hooks/hooks.json, exec
form)::

    python3 ${CLAUDE_PLUGIN_ROOT}/hooks/route_hook.py <pre|post|stop|...>

- Finds the ``judge_adapter`` code, first match wins: ``DAVI_ROUTE_SRC``;
  the ``src`` of a checkout above this file (``--plugin-dir``, so edits in
  ``src`` take effect); the plugin's own copy in ``lib`` (what a
  marketplace install runs: it copies only ``plugins/davi-route``); the
  ``src`` of the marketplace's copy of the repository.
- Options: ``DAVI_ROUTE_<KEY>`` environment variables (also from the
  ``env`` of Claude Code settings), then the plugin's settings dialog
  (``CLAUDE_PLUGIN_OPTION_<KEY>``), then the JSON file written by
  ``/davi-route:setup`` (``~/.claude/davi-route.json``, or
  ``DAVI_ROUTE_CONFIG``). The dialog holds the connection only
  (``DIALOG_KEYS``); the features and every other option live in the file.
  Dialog values for other keys, saved by an older version of the plugin,
  are dropped. The file is read on every call and its values are passed on
  as ``DAVI_ROUTE_<KEY>`` when neither of the others is set.
- Fast path: if the hook's feature is off (``route_enabled`` for pre/post,
  ``stop_verify_enabled`` for stop, either for setup) and probe mode is
  off, it exits 0 before reading stdin or importing anything.
- Needs httpx for real verifier calls. ``setup`` (SessionStart) installs it
  with ``pip install --target ${CLAUDE_PLUGIN_DATA}/pydeps`` when a feature
  is on, ``python3`` cannot import it and ``auto_install_deps`` is not off.
  The hooks add that directory to ``sys.path``; ``${CLAUDE_PLUGIN_DATA}/venv``
  is still tried. Without httpx a hook logs ``dependency_missing`` and exits
  0 (fail-open).
"""

import json
import os
import re
import subprocess
import sys
import time

CONFIG_FILE = 'davi-route.json'
CONFIG_KEY = re.compile(r'^[a-z][a-z0-9_]*$')
KINDS = ('pre', 'post', 'stop', 'subagent_stop', 'setup')
REEXEC_GUARD = '_DAVI_ROUTE_HOOK_REEXEC'
DIALOG_PREFIX = 'CLAUDE_PLUGIN_OPTION_'
OPTION_PREFIXES = ('DAVI_ROUTE_', DIALOG_PREFIX)
# plugin.json userConfig: the connection; the rest is set by the setup skill
DIALOG_KEYS = (
    'davi_base_url',
    'davi_api_key',
    'typesafe_api_key',
    'anthropic_api_key',
)
TRUE_WORDS = ('1', 'true', 'yes', 'on')
OFF_WORDS = ('', '0', 'false', 'no', 'off')
VENV_PYTHONS = (('venv', 'bin', 'python'), ('venv', 'Scripts', 'python.exe'))
DEPENDENCY = 'httpx>=0.24,<1'
DEPS_DIR = 'pydeps'
SETUP_LOCK_SECONDS = 600
PIP_TIMEOUT_SECONDS = 180


def config_path():
    configured = os.environ.get('DAVI_ROUTE_CONFIG', '').strip()
    if configured:
        return configured
    home = os.environ.get('CLAUDE_CONFIG_DIR', '').strip()
    home = home or os.path.join(os.path.expanduser('~'), '.claude')
    return os.path.join(home, CONFIG_FILE)


def _env_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, tuple)):
        return ','.join(str(item) for item in value)
    return str(value)


def read_config(path):
    """The option dict in ``path``; {} when missing, unreadable or invalid."""
    try:
        with open(path, encoding='utf-8') as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def drop_stale_options():
    """Forget dialog values for keys the settings dialog no longer has.

    Claude Code keeps exporting options saved by an older version of the
    plugin (``route_enabled`` and the others before 0.7.0); they would
    switch features on that the user can no longer see.
    """
    kept = {DIALOG_PREFIX + key.upper() for key in DIALOG_KEYS}
    for name in list(os.environ):
        if name.startswith(DIALOG_PREFIX) and name not in kept:
            del os.environ[name]


def dialog_value(text):
    """A ``${user_config.KEY}`` value a skill passed on; None when unset
    (Claude Code leaves the placeholder in place) or empty."""
    text = (text or '').strip()
    if not text or '${' in text:
        return None
    return text


def load_config_file():
    """Pass the file's options on as ``DAVI_ROUTE_<KEY>`` variables, for
    keys set neither by such a variable nor by the settings dialog."""
    for key, value in read_config(config_path()).items():
        if value is None or not CONFIG_KEY.match(str(key)):
            continue
        names = ('DAVI_ROUTE_' + key.upper(), DIALOG_PREFIX + key.upper())
        if not any(os.environ.get(name, '').strip() for name in names):
            os.environ[names[0]] = _env_value(value)


def _opt(key):
    for prefix in OPTION_PREFIXES:
        value = os.environ.get(prefix + key.upper(), '').strip().lower()
        if value:
            return value
    return ''


def _on(key):
    # an invalid value runs main(), which reports it
    return _opt(key) not in OFF_WORDS


def install_allowed():
    # auto_install_deps defaults to on: only an explicit off disables it
    return not _opt('auto_install_deps') or _on('auto_install_deps')


def feature_on(kind):
    if _opt('probe') in TRUE_WORDS:
        return kind != 'setup'
    if kind == 'setup':
        enabled = _on('route_enabled') or _on('stop_verify_enabled')
        return enabled and install_allowed()
    if kind in ('pre', 'post'):
        return _on('route_enabled')
    return _on('stop_verify_enabled')


def _has_package(src, package):
    return os.path.isdir(os.path.join(src, 'judge_adapter', package))


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


def _marketplace_src(here, package):
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
        if root and _has_package(os.path.join(root, 'src'), package):
            return os.path.join(root, 'src')
    return None


def find_src(package='routing'):
    """The ``src`` folder whose ``judge_adapter`` has ``package``."""
    configured = os.environ.get('DAVI_ROUTE_SRC', '').strip()
    if configured and os.path.isdir(os.path.join(configured, 'judge_adapter')):
        return configured
    here = os.path.dirname(os.path.abspath(__file__))
    directory = here
    for _ in range(6):
        candidate = os.path.join(directory, 'src')
        if _has_package(candidate, package):
            return candidate
        directory = os.path.dirname(directory)
    bundled = os.path.join(os.path.dirname(here), 'lib')
    if _has_package(bundled, package):
        return bundled
    return _marketplace_src(here, package)


def _venv_python():
    data = os.environ.get('CLAUDE_PLUGIN_DATA')
    if not data:
        return None
    for parts in VENV_PYTHONS:
        path = os.path.join(data, *parts)
        if os.path.isfile(path):
            return path
    return None


def deps_dir():
    data = os.environ.get('CLAUDE_PLUGIN_DATA')
    return os.path.join(data, DEPS_DIR) if data else None


def add_deps_path():
    deps = deps_dir()
    if deps and os.path.isdir(deps) and deps not in sys.path:
        sys.path.insert(0, deps)


def _note(kind, status, detail):
    record = {
        'event': 'davi-route',
        'kind': kind,
        'status': status,
        'detail': detail,
    }
    sys.stderr.write(json.dumps(record) + '\n')


def _take_lock(path, now):
    """One installer at a time; a lock older than the limit is stale."""
    try:
        os.mkdir(path)
        return True
    except FileExistsError:
        try:
            if now - os.path.getmtime(path) < SETUP_LOCK_SECONDS:
                return False
            os.rmdir(path)
            os.mkdir(path)
            return True
        except OSError:
            return False


def install_dependency(deps, run=subprocess.run, python=None, now=time.time):
    """pip-install httpx into ``deps``. -> (status, detail); never raises."""
    python = python or sys.executable
    parent = os.path.dirname(deps)
    lock = deps + '.lock'
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as error:
        return 'install_failed', type(error).__name__
    if not _take_lock(lock, now()):
        return 'install_busy', 'another session is installing'
    base = [
        python,
        '-m',
        'pip',
        'install',
        '--quiet',
        '--disable-pip-version-check',
        '--no-input',
        '--upgrade',
        '--target',
        deps,
        DEPENDENCY,
    ]
    try:
        detail = ''
        # --target leaves the system packages alone; some Pythons still
        # refuse (PEP 668) unless told so
        for extra in ([], ['--break-system-packages']):
            try:
                result = run(
                    base + extra,
                    capture_output=True,
                    text=True,
                    timeout=PIP_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError) as error:
                return 'install_failed', type(error).__name__
            if result.returncode == 0:
                return 'installed', f'{DEPENDENCY} -> {deps}'
            detail = (result.stderr or '').strip().splitlines()[-1:]
            detail = detail[0][:200] if detail else f'pip {result.returncode}'
        return 'install_failed', detail
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def setup(kind):
    import importlib.util

    add_deps_path()
    if importlib.util.find_spec('httpx') is not None:
        return 0
    python = _venv_python()
    if python:
        check = subprocess.run(
            [python, '-c', 'import httpx'], capture_output=True, timeout=30
        )
        if check.returncode == 0:
            return 0
    deps = deps_dir()
    if deps is None:
        _note(kind, 'install_failed', 'CLAUDE_PLUGIN_DATA is not set')
        return 0
    status, detail = install_dependency(deps)
    _note(kind, status, detail)
    return 0


def main(kind):
    import importlib.util

    if kind == 'setup':
        return setup(kind)
    add_deps_path()
    if importlib.util.find_spec('httpx') is None:
        python = _venv_python()
        if python and not os.environ.get(REEXEC_GUARD):
            os.environ[REEXEC_GUARD] = '1'
            os.execv(python, [python, os.path.abspath(__file__), kind])
        _note(
            kind,
            'dependency_missing',
            'httpx is not importable by python3 (turn auto_install_deps on '
            'and start a new session, or pip install "httpx>=0.24,<1")',
        )
        return 0
    src = find_src()
    if src is None:
        _note(
            kind,
            'src_missing',
            'could not find judge_adapter (checkout, plugin lib or '
            'marketplace clone); set DAVI_ROUTE_SRC',
        )
        return 0
    sys.path.insert(0, src)
    from judge_adapter.routing.hooks import main as hook_main

    return hook_main(kind)


if __name__ == '__main__':
    hook_kind = sys.argv[1] if len(sys.argv) > 1 else ''
    drop_stale_options()
    load_config_file()
    if hook_kind not in KINDS or not feature_on(hook_kind):
        sys.exit(0)
    try:
        main(hook_kind)
    except Exception as error:  # noqa: BLE001 - fail-open
        _note(hook_kind, 'launcher_error', type(error).__name__)
    sys.exit(0)
