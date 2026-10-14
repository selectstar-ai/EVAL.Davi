#!/usr/bin/env python3
# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Launcher for ``judge_adapter.routing.hooks`` (davi-route plugin). Standard library only until the hook runs.

Usage (hooks/hooks.json, exec form): ``python3 ${CLAUDE_PLUGIN_ROOT}/hooks/route_hook.py <pre|post|stop|subagent_stop>``

- Finds the repository's ``src`` by walking up from this file (the plugin lives in ``plugins/davi-route`` of the
  EVAL.Davi checkout), or uses ``DAVI_ROUTE_SRC`` if set.
- Fast path: if the hook's feature is off (``route_enabled`` for pre/post, ``stop_verify_enabled`` for stop) and probe
  mode is off, it exits 0 before reading stdin or importing anything.
- Needs httpx for real verifier calls: if ``python3`` cannot import it, ``${CLAUDE_PLUGIN_DATA}/venv`` is tried;
  otherwise it logs ``dependency_missing`` and exits 0 (fail-open). Never installs anything.
"""
import json
import os
import sys

KINDS = ('pre', 'post', 'stop', 'subagent_stop')
REEXEC_GUARD = '_DAVI_ROUTE_HOOK_REEXEC'


def _opt(key):
    for p in ('DAVI_ROUTE_', 'CLAUDE_PLUGIN_OPTION_'):
        v = os.environ.get(p + key.upper(), '').strip().lower()
        if v:
            return v
    return ''


def feature_on(kind):
    if _opt('probe') in ('1', 'true', 'yes', 'on'):
        return True
    key = 'route_enabled' if kind in ('pre', 'post') else 'stop_verify_enabled'
    v = _opt(key)
    return v not in ('', '0', 'false', 'no', 'off')     # an invalid value runs main(), which reports it


def find_src():
    env = os.environ.get('DAVI_ROUTE_SRC', '').strip()
    if env and os.path.isdir(os.path.join(env, 'judge_adapter')):
        return env
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        cand = os.path.join(d, 'src')
        if os.path.isdir(os.path.join(cand, 'judge_adapter', 'routing')):
            return cand
        d = os.path.dirname(d)
    return None


def _venv_python():
    data = os.environ.get('CLAUDE_PLUGIN_DATA')
    if not data:
        return None
    for rel in (('venv', 'bin', 'python'), ('venv', 'Scripts', 'python.exe')):
        p = os.path.join(data, *rel)
        if os.path.isfile(p):
            return p
    return None


def _note(kind, status, detail):
    sys.stderr.write(json.dumps({'event': 'davi-route', 'kind': kind, 'status': status, 'detail': detail}) + '\n')


def main(kind):
    import importlib.util
    if importlib.util.find_spec('httpx') is None:
        py = _venv_python()
        if py and not os.environ.get(REEXEC_GUARD):
            os.environ[REEXEC_GUARD] = '1'
            os.execv(py, [py, os.path.abspath(__file__), kind])
        _note(kind, 'dependency_missing', 'httpx is not importable by python3 (pip install "httpx>=0.24,<1")')
        return 0
    src = find_src()
    if src is None:
        _note(kind, 'src_missing', 'could not find src/judge_adapter/routing; set DAVI_ROUTE_SRC')
        return 0
    sys.path.insert(0, src)
    from judge_adapter.routing.hooks import main as hook_main
    return hook_main(kind)


if __name__ == '__main__':
    k = sys.argv[1] if len(sys.argv) > 1 else ''
    if k not in KINDS or not feature_on(k):
        sys.exit(0)
    try:
        main(k)
    except Exception as e:  # noqa: BLE001 - fail-open: never break Claude Code
        _note(k, 'launcher_error', type(e).__name__)
    sys.exit(0)
