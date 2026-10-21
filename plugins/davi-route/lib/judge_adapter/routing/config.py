# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Option and key lookup shared by the verifiers, the hooks and the CLI.

An option ``key`` is read from ``DAVI_ROUTE_<KEY>`` first (an explicit
per-run override from the harness or the shell), then from
``CLAUDE_PLUGIN_OPTION_<KEY>`` (Claude Code's export of the plugin's
userConfig). Empty strings count as unset. Keys are never read here: a
key provider reads them lazily, at the first request.
"""

from __future__ import annotations

from ..keys import CredentialMissing

OVERRIDE_PREFIX = 'DAVI_ROUTE_'
OPTION_PREFIX = 'CLAUDE_PLUGIN_OPTION_'
TRUE_WORDS = ('1', 'true', 'yes', 'on')
FALSE_WORDS = ('0', 'false', 'no', 'off')

SECRET_ENVS = {
    'davi': (
        'DAVI_ROUTE_RUNPOD_API_KEY',
        'CLAUDE_PLUGIN_OPTION_RUNPOD_API_KEY',
        'RUNPOD_API_KEY',
    ),
    'davi_systemone': (
        'DAVI_ROUTE_DAVI_API_KEY',
        'CLAUDE_PLUGIN_OPTION_DAVI_API_KEY',
        'DAVI_API_KEY',
    ),
    'jev': (
        'DAVI_ROUTE_TYPESAFE_API_KEY',
        'CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY',
        'TYPESAFE_API_KEY',
        'JEV_API_KEY',
    ),
    'claude': (
        'DAVI_ROUTE_ANTHROPIC_API_KEY',
        'CLAUDE_PLUGIN_OPTION_ANTHROPIC_API_KEY',
        'ANTHROPIC_API_KEY',
    ),
}
ENDPOINT_ENVS = (
    'DAVI_ROUTE_CHOICE_ENDPOINT_ID',
    'CLAUDE_PLUGIN_OPTION_CHOICE_ENDPOINT_ID',
    'JUDGE_ADAPTER_CHOICE_ENDPOINT_ID',
)


class ConfigError(ValueError):
    """An option holds a value the routing code cannot use."""


def unquote(value):
    """Strip one pair of matching quotes, as left by a pasted ``.env``."""
    value = (value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        return value[1:-1].strip()
    return value


def opt(env, key, default=None):
    """``DAVI_ROUTE_<KEY>``, then ``CLAUDE_PLUGIN_OPTION_<KEY>``."""
    for prefix in (OVERRIDE_PREFIX, OPTION_PREFIX):
        value = env.get(prefix + key.upper())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def opt_number(env, key, default=None, low=None, high=None, cast=float):
    """A numeric option, optionally bounded to ``[low, high]``."""
    raw = opt(env, key)
    if raw is None:
        return default
    try:
        value = cast(float(raw))
    except ValueError:
        raise ConfigError(f'{key} must be a number') from None
    if (low is not None and value < low) or (
        high is not None and value > high
    ):
        raise ConfigError(f'{key} must be in [{low}, {high}]')
    return value


def opt_flag(env, key, default):
    """A boolean option written as true/false, yes/no, on/off or 1/0."""
    raw = opt(env, key)
    if raw is None:
        return default
    word = raw.lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ConfigError(f'{key} must be true or false')


def opt_choice(env, key, default, allowed):
    """A lower-cased option that must be one of ``allowed``."""
    value = (opt(env, key) or default).lower()
    if value not in allowed:
        raise ConfigError(f'{key} must be one of {allowed}')
    return value


def first_env(env, names):
    """The first non-empty environment value among ``names``, or None."""
    for name in names:
        value = env.get(name, '')
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def lazy_key(env, names, required=True):
    """A key provider: reads ``names`` only when called.

    A required key that is missing raises ``CredentialMissing`` (never
    the value); an optional one returns None.
    """

    def provider():
        value = first_env(env, names)
        if value is None and required:
            raise CredentialMissing(f'none of {", ".join(names)} is set')
        return value

    return provider
