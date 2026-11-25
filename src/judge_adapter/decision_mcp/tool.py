# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""The ``decide`` tool without the MCP SDK: arguments in, result out.

    decider = Decider.from_env(os.environ)
    decider.decide({'type': 'choice', 'state': ..., ...}) -> dict

The judge (Davi or Jev), its model, URL, key, timeout and ``noul_mode``
are fixed when the server starts; the tool arguments never choose them.
Every failure is a ``DecisionError`` whose message holds no state, file,
answer or key text.
"""

from __future__ import annotations

import json
import os

from ..decision import (
    ChoiceQuestion,
    DecisionError,
    NoulQuestion,
    build_client,
    with_options_file,
)
from ..decision.settings import PROVIDERS, option
from .files import StateFiles

TYPES = ('choice', 'noul')
ARGUMENTS = (
    'type',
    'instructions',
    'criteria',
    'state',
    'state_files',
    'threshold',
)
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_CHOICE_OPTIONS = 32
DEFAULT_THRESHOLD = 0.5


def _invalid(message):
    return DecisionError('invalid_request', message)


def argument_bytes(arguments):
    """The UTF-8 JSON size of the tool arguments."""
    try:
        text = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        raise _invalid('the arguments are not JSON') from None
    return len(text.encode('utf-8'))


def check_arguments(arguments):
    """Size and names, before any field is read."""
    if not isinstance(arguments, dict):
        raise _invalid('the arguments must be an object')
    if argument_bytes(arguments) > MAX_ARGUMENT_BYTES:
        raise _invalid(
            f'the arguments are over {MAX_ARGUMENT_BYTES // 1024} KiB; '
            'pass long material as state_files'
        )
    unknown = sorted(set(arguments) - set(ARGUMENTS))
    if unknown:
        shown = [
            name
            for name in unknown
            if isinstance(name, str) and name.isidentifier() and len(name) < 41
        ]
        raise _invalid(
            'unexpected argument(s)'
            + (f': {", ".join(shown)}' if shown == unknown else '')
        )


class Decider:
    """One judge client, one set of allowed roots, many ``decide`` calls."""

    def __init__(self, client, files=None):
        self._client = client
        self._files = files or StateFiles()

    @classmethod
    def from_env(
        cls,
        env,
        *,
        judge=None,
        noul_mode=None,
        timeout=None,
        roots=None,
        transport=None,
    ):
        """Server-start settings: arguments, then ``DAVI_ROUTE_<KEY>``,
        ``CLAUDE_PLUGIN_OPTION_<KEY>`` and ``~/.claude/davi-route.json``.
        Raises ``ValueError`` for a bad setting (no key is read here)."""
        env = with_options_file(env)
        judge = (judge or option(env, 'decision_judge', 'davi')).lower()
        if judge not in PROVIDERS:
            raise ValueError(f'the judge must be one of {PROVIDERS}')
        if roots is None:
            raw = option(env, 'decision_allowed_roots')
            roots = [r for r in raw.split(os.pathsep) if r] if raw else None
        files = StateFiles(roots)
        client = build_client(
            judge,
            env,
            noul_mode=noul_mode,
            timeout=timeout,
            max_options=MAX_CHOICE_OPTIONS,
            transport=transport,
        )
        return cls(client, files)

    @property
    def provider(self):
        return self._client.provider

    @property
    def model(self):
        return self._client.model

    @property
    def noul_mode(self):
        return self._client.noul_mode

    @property
    def roots(self):
        return self._files.roots

    def decide(self, arguments):
        """Validated tool arguments -> the result object."""
        check_arguments(arguments)
        question = self._question(arguments)
        state = self._files.combine(
            arguments.get('state'), arguments.get('state_files')
        )
        reply = self._client.ask(state, {'q': question})
        result = reply.answers['q'].to_dict()
        result.pop('noul_mode', None)
        meta = {'provider': self.provider, 'model': self.model}
        if question.kind == 'noul':
            meta['noul_mode'] = self.noul_mode
        meta['latency_ms'] = reply.latency_ms
        result['meta'] = meta
        return result

    def close(self):
        self._client.close()

    def _question(self, arguments):
        kind = arguments.get('type')
        if kind not in TYPES:
            raise _invalid(f'type must be one of {", ".join(TYPES)}')
        state_files = arguments.get('state_files')
        if state_files is not None and not isinstance(state_files, list):
            raise _invalid('state_files must be a list of paths')
        state = arguments.get('state')
        if state is not None and not isinstance(state, str):
            raise _invalid('state must be a string')
        criteria = arguments.get('criteria')
        threshold = arguments.get('threshold')
        if kind == 'choice':
            if threshold is not None:
                raise _invalid('threshold is only for noul')
            if isinstance(criteria, dict) and len(criteria) > (
                MAX_CHOICE_OPTIONS
            ):
                raise _invalid(
                    f'a choice takes at most {MAX_CHOICE_OPTIONS} options'
                )
            return ChoiceQuestion(arguments.get('instructions'), criteria)
        return NoulQuestion(
            arguments.get('instructions'),
            criteria,
            DEFAULT_THRESHOLD if threshold is None else threshold,
        )
