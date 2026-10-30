# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""``POST /v1/systemone`` clients for Davi and Jev.

    client.ask(state, {'q1': question, ...}) -> Reply
    client.decide(state, question) -> answer record

    SystemOneClient (abstract)  request, status mapping, strict reading
    ├── DaviClient              Davi at a configured URL, key optional
    └── JevClient               TypeSafe Jev, Bearer key required

Rules shared with the routing verifiers: the key is read at the first
request and scrubbed from every message, retries are 0, redirects are not
followed, and one client is reused until ``close``. Every failure is a
``DecisionError`` with one of ``ERROR_CODES``; a false or low answer is
never used to hide an error. Several questions go in ONE request; either
all of them read cleanly or the call fails.
"""

from __future__ import annotations

import json
import re
import threading
import time
from abc import ABC, abstractmethod

import httpx

from ..keys import CredentialMissing, Scrubber
from .types import NOUL_MODES, DecisionError, Question, Reply

PATH = '/v1/systemone'
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_OPTIONS = 512
JEV_BASE_URL = 'https://api.typesafe.ai'
JEV_DEFAULT_MODEL = 'jev-latest'
DAVI_DEFAULT_MODEL = 'davi'

_ERROR_NAME_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]{0,40}):')


def _check_state(state):
    if isinstance(state, str):
        if not state.strip():
            raise DecisionError('invalid_request', 'state is empty')
    elif isinstance(state, (dict, list)):
        if not state:
            raise DecisionError('invalid_request', 'state is empty')
    else:
        raise DecisionError(
            'invalid_request', 'state must be a string, object or list'
        )


def _server_error_name(data):
    """The exception class name a server put first in its error, if any."""
    text = data.get('error') if isinstance(data, dict) else None
    match = _ERROR_NAME_RE.match(text) if isinstance(text, str) else None
    return match.group(1) if match else None


class SystemOneClient(ABC):
    """Shared request and reading rules; subclasses set the headers."""

    provider = 'systemone'

    def __init__(
        self,
        base_url,
        key_provider,
        *,
        model,
        noul_mode='choice',
        timeout=DEFAULT_TIMEOUT,
        max_options=DEFAULT_MAX_OPTIONS,
        transport=None,
        clock=time.perf_counter,
    ):
        if noul_mode not in NOUL_MODES:
            raise ValueError(f'noul_mode must be one of {NOUL_MODES}')
        if not base_url:
            raise ValueError('a base URL is required')
        self._base_url = base_url.rstrip('/')
        self._key_provider = key_provider
        self._model = model
        self._noul_mode = noul_mode
        self._timeout = timeout
        self._max_options = max_options
        self._transport = transport
        self._clock = clock
        self._scrubber = Scrubber()
        self._client = None
        self._lock = threading.Lock()

    @property
    def model(self):
        return self._model

    @property
    def noul_mode(self):
        return self._noul_mode

    @property
    def base_url(self):
        return self._base_url

    @abstractmethod
    def _headers(self, key):
        """Request headers for ``key`` (which may be None)."""

    # --- public -------------------------------------------------------------
    def decide(self, state, question):
        """One question -> its answer record (``ChoiceAnswer`` or
        ``NoulAnswer``)."""
        return self.ask(state, {'q': question}).answers['q']

    def ask(self, state, questions):
        """Several questions about one state, in one request -> ``Reply``."""
        body = self.body(state, questions)
        started = self._clock()
        response = self._send(body)
        latency_ms = round((self._clock() - started) * 1000.0, 1)
        data = self._json(response)
        answers = data.get('answers')
        if not isinstance(answers, dict):
            raise DecisionError('format_error', 'the reply has no answers')
        read = {
            key: question.read(
                answers.get(key), self._noul_mode, f'answer {key!r}'
            )
            for key, question in questions.items()
        }
        usage = data.get('usage')
        meta = {'noul_mode': self._noul_mode}
        if isinstance(data.get('latency_ms'), (int, float)):
            meta['server_latency_ms'] = data['latency_ms']
        return Reply(
            read,
            self.provider,
            data.get('model') if isinstance(data.get('model'), str) else None,
            latency_ms,
            usage if isinstance(usage, dict) else None,
            meta,
        )

    def body(self, state, questions):
        """The request body; validates before anything is sent."""
        _check_state(state)
        if not isinstance(questions, dict) or not questions:
            raise DecisionError('invalid_request', 'no questions')
        wire = {}
        for key, question in questions.items():
            if not isinstance(key, str) or not key.strip():
                raise DecisionError('invalid_request', 'empty question id')
            if not isinstance(question, Question):
                raise DecisionError(
                    'invalid_request', f'question {key!r} is not a Question'
                )
            if question.option_count(self._noul_mode) > self._max_options:
                raise DecisionError(
                    'invalid_request',
                    f'question {key!r} has more than '
                    f'{self._max_options} options',
                )
            wire[key] = question.wire(self._noul_mode)
        body = {'state': state, 'model': self._model, 'questions': wire}
        try:
            json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError):
            raise DecisionError(
                'invalid_request', 'state is not JSON-serialisable'
            ) from None
        return body

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # --- plumbing -----------------------------------------------------------
    def _http(self):
        with self._lock:
            if self._client is None:
                try:
                    key = self._key_provider()
                except CredentialMissing as error:
                    raise DecisionError(
                        'credential_missing', str(error)
                    ) from None
                self._scrubber.set_secret(key)
                transport = self._transport or httpx.HTTPTransport(retries=0)
                self._client = httpx.Client(
                    transport=transport,
                    timeout=httpx.Timeout(self._timeout),
                    follow_redirects=False,
                    headers=self._headers(key),
                )
            return self._client

    def _send(self, body):
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        client = self._http()
        try:
            return client.post(self._base_url + PATH, content=payload)
        except httpx.TimeoutException:
            raise DecisionError(
                'timeout', f'no reply within {self._timeout:g} s'
            ) from None
        except httpx.HTTPError as error:
            raise DecisionError(
                'provider_error',
                self._scrubber.text(f'request failed: {type(error).__name__}'),
            ) from None

    def _json(self, response):
        status = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = None
        if status == 200:
            if not isinstance(data, dict):
                raise DecisionError('format_error', 'the reply is not JSON')
            return data
        name = _server_error_name(data)
        where = f'HTTP {status}' + (f', {name}' if name else '')
        if status in (401, 403):
            raise DecisionError(
                'credential_missing', f'the server refused the key ({where})'
            )
        if status in (400, 404, 413, 422):
            raise DecisionError(
                'invalid_request', f'the server rejected the request ({where})'
            )
        raise DecisionError('provider_error', f'the server failed ({where})')


class DaviClient(SystemOneClient):
    """Davi behind ``base_url``. A self-served Davi may need no key; the
    request ``model`` is echoed back by the server and selects nothing."""

    provider = 'davi'

    def __init__(
        self, base_url, key_provider=None, model=DAVI_DEFAULT_MODEL, **kwargs
    ):
        super().__init__(
            base_url, key_provider or (lambda: None), model=model, **kwargs
        )

    def _headers(self, key):
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        if key:
            headers['Authorization'] = f'Bearer {key}'
        return headers


class JevClient(SystemOneClient):
    """TypeSafe Jev. The key is required."""

    provider = 'jev'

    def __init__(
        self,
        key_provider,
        model=JEV_DEFAULT_MODEL,
        base_url=JEV_BASE_URL,
        **kwargs,
    ):
        super().__init__(base_url, key_provider, model=model, **kwargs)

    def _headers(self, key):
        if not key:
            raise DecisionError('credential_missing', 'no Jev key')
        return {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Authorization': f'Bearer {key}',
        }
