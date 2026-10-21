# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Contracts shared by the davi plugin hooks (``plugin_hook``, ``query_hook``, ``prompt_signal``), the toggle module
and ``hooks/davi_toggle.py``. Standard library only.

Import cost: ``toggles`` imports this module on the launchers' fast path, where it is loaded under the private
``_davi_stub`` package (no package ``__init__``, no httpx). At import time it therefore imports only ``os`` and
``plugin_names``; ``json``, ``tempfile``, ``threading`` and ``unicodedata`` are imported inside the functions that
use them, so an "off" launcher never loads them.

Only contracts that were identical in every copy live here. Deliberately NOT merged (they differ on purpose):

- ``toggles.flag_value`` is lenient (an invalid ``enabled`` counts as on and is reported by ``status``), while the
  hooks' ``parse_flag`` raises ``ConfigError`` (status ``config_error``, nothing changed). Both use the same words.
- ``hooks/query_options_hook.py`` keeps its own env reading (``_opt``, lower-cased, ``os.environ`` only): it is the
  fallback for when the toggle module (and so this module) cannot be loaded at all.
- ``toggles.data_dir`` strips and expands ``~`` in ``CLAUDE_PLUGIN_DATA`` and falls back to
  ``~/.claude/plugins/data/<id>``; ``state_dir`` joins the raw value and falls back to the system temp directory.
- The log line encoding stays per caller: the PostToolUse / PreToolUse hooks write ``ensure_ascii=False``, the
  UserPromptSubmit hook ``ensure_ascii=True`` (``emit_json`` takes it as an argument).
- The choice-endpoint client setup (AdapterConfig, lazy key provider, estimate cap) of ``plugin_hook.process`` and
  ``query_hook._Caller`` stays in each hook: it needs the package (httpx) and its cap counts different things
  (scored documents vs. pending decisions).
"""
import os

from . import plugin_names as N

OPTION_ENV = 'CLAUDE_PLUGIN_OPTION_'           # Claude Code exports every userConfig option as this + KEY.upper()
OPTION_PREFIXES = (OPTION_ENV, N.ENV_PREFIX)   # then the fallback for running a hook outside a plugin
KEY_ENVS = (OPTION_ENV + 'RUNPOD_API_KEY', 'RUNPOD_API_KEY')
ENDPOINT_ENVS = (OPTION_ENV + 'CHOICE_ENDPOINT_ID', 'JUDGE_ADAPTER_CHOICE_ENDPOINT_ID')
TRUE_WORDS, FALSE_WORDS = ('1', 'true', 'yes', 'on'), ('0', 'false', 'no', 'off')

# Verified search tool (tavily-mcp 0.2.22 registers only "tavily_search"). hooks/hooks.json uses this verbatim.
TAVILY_TOOL = 'tavily_search'
KNOWN_TOOLS_MATCHER = '^mcp__[A-Za-z0-9_-]+__' + TAVILY_TOOL + '$'


class ConfigError(ValueError):
    """An invalid userConfig value: the hooks log ``config_error`` and change nothing."""


# --- options ---------------------------------------------------------------------------------------------------------
def option(env, key):
    """userConfig option ``key``: CLAUDE_PLUGIN_OPTION_<KEY>, then JUDGE_ADAPTER_HOOK_<KEY>. Empty -> None."""
    for p in OPTION_PREFIXES:
        v = env.get(p + key.upper())
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def present(env, names):
    """Name of the first set variable (presence only; the value is not returned)."""
    for n in names:
        v = env.get(n)
        if isinstance(v, str) and v.strip():
            return n
    return None


def parse_flag(v, name, default):
    """Strict boolean option (hooks): None -> ``default``; anything but the fixed words raises ConfigError."""
    if v is None:
        return default
    s = v.strip().lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    raise ConfigError(f'{name} must be true or false')


def parse_num(v, name, default, lo, hi, cast=float):
    if v is None:
        return default
    try:
        x = float(v)
    except ValueError:
        raise ConfigError(f'{name} must be a number') from None
    if cast is int:
        if x != int(x):
            raise ConfigError(f'{name} must be an integer')
        x = int(x)
    if not lo <= x <= hi:
        raise ConfigError(f'{name} must be in [{lo}, {hi}]')
    return x


# --- local state and text --------------------------------------------------------------------------------------------
def state_dir(env, dirname):
    """``${CLAUDE_PLUGIN_DATA}/<dirname>``, else ``<system temp>/<plugin>-<dirname>-<uid>``."""
    data = env.get('CLAUDE_PLUGIN_DATA')
    if isinstance(data, str) and data.strip():
        return os.path.join(data, dirname)
    import tempfile
    uid = os.getuid() if hasattr(os, 'getuid') else 'user'
    return os.path.join(tempfile.gettempdir(), f'{N.PLUGIN_NAME}-{dirname}-{uid}')


def normalize_text(text):
    """NFKC, casefold, whitespace runs collapsed (prompt signal matching and the gate's retry key)."""
    import unicodedata
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


# --- process -----------------------------------------------------------------------------------------------------------
def emit_json(rec, stderr, log_path=None, *, ensure_ascii):
    """One JSON log line on ``stderr`` and, if ``log_path`` is set, appended to that file. Never raises for I/O.
    ``ensure_ascii`` has no default on purpose: the PostToolUse/PreToolUse hooks log non-ASCII as is (False), the
    UserPromptSubmit hook logs ASCII (True); each caller states it."""
    import json
    line = json.dumps(rec, ensure_ascii=ensure_ascii, sort_keys=True)
    try:
        print(line, file=stderr)
    except Exception:  # noqa: BLE001
        pass
    if log_path:
        try:
            with open(os.path.expanduser(log_path), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass


def run_with_budget(fn, seconds):
    """Run ``fn`` in a daemon thread. -> (finished, value). An exception inside ``fn`` counts as finished with value
    ``('error', <exception class name>)``; a non-Exception exit (e.g. SystemExit) gives ``('error', None)``."""
    import threading
    box = {}

    def target():
        try:
            box['value'] = fn()
        except Exception as e:  # noqa: BLE001 - fail-open
            box['error'] = type(e).__name__
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        return False, None
    return True, box.get('value', ('error', box.get('error')))
