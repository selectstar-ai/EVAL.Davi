# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Build a decision client from environment-style options.

Option ``key`` is read from ``DAVI_ROUTE_<KEY>``, then
``CLAUDE_PLUGIN_OPTION_<KEY>`` (the same precedence as the routing hooks).
``with_options_file`` adds the options saved by ``/davi-route:setup``
(``~/.claude/davi-route.json``) as unset ``DAVI_ROUTE_<KEY>`` values, as the
plugin launcher does. Keys are never read here: the client reads them at
its first request. The decision core does not import the routing package,
so the variable names are repeated here (a test keeps them in step).
"""

from __future__ import annotations

import json
import os

from ..keys import CredentialMissing
from .client import (
    DAVI_DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    JEV_BASE_URL,
    JEV_DEFAULT_MODEL,
    DaviClient,
    JevClient,
)
from .types import NOUL_MODES

PROVIDERS = ('davi', 'jev')
OPTION_PREFIXES = ('DAVI_ROUTE_', 'CLAUDE_PLUGIN_OPTION_')
DAVI_URL_ENVS = (
    'DAVI_ROUTE_DAVI_BASE_URL',
    'CLAUDE_PLUGIN_OPTION_DAVI_BASE_URL',
    'DAVI_BASE_URL',
)
DAVI_KEY_ENVS = (
    'DAVI_ROUTE_DAVI_API_KEY',
    'CLAUDE_PLUGIN_OPTION_DAVI_API_KEY',
    'DAVI_API_KEY',
)
JEV_KEY_ENVS = (
    'DAVI_ROUTE_TYPESAFE_API_KEY',
    'CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY',
    'TYPESAFE_API_KEY',
    'JEV_API_KEY',
)
CONFIG_FILE = 'davi-route.json'


def _clean(value):
    value = (value or '').strip() if isinstance(value, str) else ''
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1].strip()
    return value


def option(env, key, default=None):
    """``DAVI_ROUTE_<KEY>``, then ``CLAUDE_PLUGIN_OPTION_<KEY>``."""
    for prefix in OPTION_PREFIXES:
        value = _clean(env.get(prefix + key.upper()))
        if value:
            return value
    return default


def first(env, names):
    """The first non-empty value among ``names``, or None."""
    for name in names:
        value = _clean(env.get(name))
        if value:
            return value
    return None


def key_provider(env, names, required):
    """Reads ``names`` only when called; never returns or raises the value."""

    def provider():
        value = first(env, names)
        if value is None and required:
            raise CredentialMissing(f'none of {", ".join(names)} is set')
        return value

    return provider


def config_path(env):
    configured = _clean(env.get('DAVI_ROUTE_CONFIG'))
    if configured:
        return configured
    home = _clean(env.get('CLAUDE_CONFIG_DIR'))
    home = home or os.path.join(os.path.expanduser('~'), '.claude')
    return os.path.join(home, CONFIG_FILE)


def with_options_file(env, path=None):
    """A copy of ``env`` plus the saved options as unset
    ``DAVI_ROUTE_<KEY>``."""
    merged = dict(env)
    try:
        with open(path or config_path(env), encoding='utf-8') as file:
            data = json.load(file)
    except (OSError, ValueError):
        return merged
    if not isinstance(data, dict):
        return merged
    for key, value in data.items():
        if value is None or not isinstance(key, str) or not key.isidentifier():
            continue
        name = 'DAVI_ROUTE_' + key.upper()
        if not _clean(merged.get(name)):
            merged[name] = value if isinstance(value, str) else str(value)
    return merged


def build_client(
    provider,
    env,
    *,
    noul_mode=None,
    timeout=None,
    max_options=None,
    transport=None,
):
    """A ``DaviClient`` or ``JevClient`` from options. Raises ``ValueError``
    for an unknown provider, mode or a missing Davi URL."""
    if provider not in PROVIDERS:
        raise ValueError(f'provider must be one of {PROVIDERS}')
    mode = (noul_mode or option(env, 'noul_mode', 'choice')).lower()
    if mode not in NOUL_MODES:
        raise ValueError(f'noul_mode must be one of {NOUL_MODES}')
    if timeout is None:
        raw = option(env, 'decision_timeout_seconds')
        try:
            timeout = float(raw) if raw else DEFAULT_TIMEOUT
        except ValueError:
            raise ValueError(
                'decision_timeout_seconds must be a number'
            ) from None
    common = {'noul_mode': mode, 'timeout': timeout, 'transport': transport}
    if max_options is not None:
        common['max_options'] = max_options
    if provider == 'jev':
        return JevClient(
            key_provider(env, JEV_KEY_ENVS, required=True),
            model=option(env, 'jev_model', JEV_DEFAULT_MODEL),
            base_url=option(env, 'jev_base_url', JEV_BASE_URL),
            **common,
        )
    base_url = first(env, DAVI_URL_ENVS)
    if not base_url:
        raise ValueError(
            'no Davi server URL: set DAVI_BASE_URL or run /davi-route:setup'
        )
    return DaviClient(
        base_url,
        key_provider(env, DAVI_KEY_ENVS, required=False),
        model=option(env, 'davi_model', DAVI_DEFAULT_MODEL),
        **common,
    )
