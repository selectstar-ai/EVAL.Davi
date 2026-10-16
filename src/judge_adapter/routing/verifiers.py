# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""One verifier interface for every judge.

    verifier.verify(task, candidate, rubric, kind) -> Verdict

Class hierarchy (abstraction -> inheritance -> polymorphism):

    Verifier (abstract)
    ├── FakeVerifier            in-process, reads ``[[ACCEPT=p]]``
    ├── DaviRunPodVerifier      the RunPod choice endpoint (binary kinds)
    └── HttpVerifier (abstract) lazy HTTP client, key scrubbing, pricing
        ├── ClaudeVerifier      Anthropic Messages, a verbalized 0-100
        └── SystemOneVerifier   ``POST /v1/systemone``, real probabilities
            ├── JevVerifier     TypeSafe Jev (Bearer key)
            └── DaviVerifier    Davi at ``DAVI_BASE_URL`` (key optional)

Callers only use ``verify`` and ``close``. No verifier raises: a failure is
a Verdict with ``p_accept=None`` and ``outcome`` naming the reason. Keys are
read at the first request and scrubbed from every detail string.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from ..keys import CredentialMissing, Scrubber
from .config import (
    ENDPOINT_ENVS,
    SECRET_ENVS,
    first_env,
    lazy_key,
    opt,
    opt_flag,
    opt_number,
    unquote,
)
from .questions import (
    CLAUDE_FORMAT_LINE,
    CLAUDE_TIER_FORMAT_LINE,
    DEFAULT_MAX_CHARS,
    KINDS,
    LABELS,
    PRE_ENSEMBLE,
    QUESTION_ID,
    TASK_ONLY_KINDS,
    TIER_LEVELS,
    clip,
    mean_logit_probability,
    state_of,
    tier_level,
)

__all__ = [
    'JUDGES',
    'KINDS',
    'LABELS',
    'PRE_ENSEMBLE',
    'QUESTION_ID',
    'TASK_ONLY_KINDS',
    'ClaudeVerifier',
    'CredentialMissing',
    'DaviRunPodVerifier',
    'DaviVerifier',
    'FakeVerifier',
    'HttpVerifier',
    'JevVerifier',
    'SystemOneVerifier',
    'Verdict',
    'Verifier',
    'build_verifier',
    'clip',
    'parse_percent',
    'uses_effort',
]

JUDGES = ('davi', 'jev', 'claude', 'fake')

JEV_DEFAULT_MODEL = 'jev-latest'
JEV_BASE_URL = 'https://api.typesafe.ai'
# ASSUMED Jev prices (USD per million input / output tokens); not a bill.
JEV_PRICES = (0.042, 0.0)

# Haiku 4.5 answered ~2% for right and wrong answers alike (2026-10-15).
CLAUDE_DEFAULT_MODEL = 'claude-haiku-5-5'
CLAUDE_BASE_URL = 'https://api.anthropic.com'
CLAUDE_DEFAULT_EFFORT = 'low'
ANTHROPIC_VERSION = '2023-06-01'
# List prices, USD per million input / output tokens, as checked 2026-10.
CLAUDE_PRICES = {
    'claude-haiku-4-5': (1.00, 5.00),
    'claude-haiku-5-5': (0.10, 0.50),
    'claude-sonnet-5-5': (2.00, 10.00),
    'claude-opus-5-5': (4.00, 20.00),
}
CLAUDE_FALLBACK_PRICES = CLAUDE_PRICES['claude-haiku-5-5']

_PERCENT_RE = re.compile(r'\d{1,3}(?:\.\d+)?')
_RATING_RE = re.compile(r'\b[1-5]\b')
_ACCEPT_MARK_RE = re.compile(r'\[\[ACCEPT=([0-9.]+)\]\]')
_LEGACY_SAMPLING_RE = re.compile(r'claude-(?:3|(?:haiku|sonnet|opus)-4)')
_RUNPOD_LABELS = {'opt_accept': 'accept', 'opt_escalate': 'escalate'}


@dataclass
class Verdict:
    """One judgment. ``p_accept`` is None on failure."""

    judge: str
    kind: str
    p_accept: float | None
    outcome: str  # 'ok' or a failure reason
    latency_s: float
    cost_usd: float | None  # estimate; None = unknown, never a silent 0
    model: str | None = None
    usage: dict | None = None
    detail: str | None = None
    level: float | None = None  # kind 'tier' only: difficulty in [0, 1]

    def to_dict(self):
        return dataclasses.asdict(self)


def parse_percent(text):
    """The LAST 0-100 number in a reply, as a probability.

    Newer models may write a short reason, with its own numbers, first.
    """
    found = _PERCENT_RE.findall(text or '')
    if not found:
        return None
    value = float(found[-1])
    return value / 100.0 if 0.0 <= value <= 100.0 else None


def uses_effort(model):
    """Claude 5th-generation models reject ``temperature`` and take
    ``output_config.effort`` instead."""
    return not _LEGACY_SAMPLING_RE.match(model or '')


def _is_probability(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0.0 <= value <= 1.0 + 1e-9
    )


class Verifier(ABC):
    """The judge interface: ``verify`` never raises; subclasses implement
    ``_verify`` for one kind of backend."""

    judge_name = 'verifier'

    def __init__(
        self,
        *,
        name=None,
        clock=time.perf_counter,
        max_chars=DEFAULT_MAX_CHARS,
    ):
        self._name = name or self.judge_name
        self._clock = clock
        self._max_chars = max_chars
        self._scrubber = Scrubber()

    @property
    def name(self):
        return self._name

    @property
    def max_chars(self):
        return self._max_chars

    def verify(self, task, candidate=None, rubric=None, kind='answer'):
        """Judge ``candidate`` for ``task`` (or the task alone for a
        task-only kind) and return a Verdict."""
        if kind not in KINDS:
            raise ValueError(f'kind must be one of {sorted(KINDS)}')
        if kind in TASK_ONLY_KINDS:
            candidate = None
        elif candidate is None:
            candidate = ''
        started = self._clock()
        try:
            verdict = self._verify(task, candidate, rubric, kind, started)
        except CredentialMissing as error:
            verdict = self._failure(
                kind, 'local_config', started, 0.0, str(error)
            )
        except httpx.TimeoutException as error:
            verdict = self._failure(
                kind, 'timeout', started, None, type(error).__name__
            )
        except Exception as error:  # noqa: BLE001 - a verifier never raises
            detail = f'{type(error).__name__}: {str(error)[:200]}'
            verdict = self._failure(kind, 'error', started, None, detail)
        if verdict.detail:
            verdict.detail = self._scrubber.text(verdict.detail)
        return verdict

    @abstractmethod
    def _verify(self, task, candidate, rubric, kind, started):
        """Return a Verdict; may raise (``verify`` turns it into one)."""

    def close(self):  # noqa: B027 - optional, a no-op by default
        """Release network resources (nothing by default)."""

    def _elapsed(self, started):
        return self._clock() - started

    def _failure(self, kind, outcome, started, cost, detail):
        return Verdict(
            self.name,
            kind,
            None,
            outcome,
            self._elapsed(started),
            cost,
            detail=self._scrubber.text(detail),
        )


class FakeVerifier(Verifier):
    """In-process judge for tests and dry runs. ``[[ACCEPT=p]]`` in the
    candidate (the task for a task-only kind) sets P(accept);
    ``FAKE:ERROR`` fails; otherwise ``default``. Not a model."""

    judge_name = 'fake'

    def __init__(self, default=0.5, cost_usd=0.0, name='fake', **kwargs):
        super().__init__(name=name, **kwargs)
        self._default = default
        self._cost_usd = cost_usd
        self._calls = []

    @property
    def calls(self):
        """Every request seen, in order (read-only view)."""
        return list(self._calls)

    def _verify(self, task, candidate, rubric, kind, started):
        self._calls.append(
            {
                'task': task,
                'candidate': candidate,
                'rubric': rubric,
                'kind': kind,
            }
        )
        seen = task if candidate is None else candidate
        if 'FAKE:ERROR' in (seen or ''):
            return Verdict(
                self.name,
                kind,
                None,
                'error',
                0.0,
                None,
                detail='fake error',
            )
        mark = _ACCEPT_MARK_RE.search(seen or '')
        p = float(mark.group(1)) if mark else self._default
        level = 1.0 - p if kind == 'tier' else None
        return Verdict(
            self.name,
            kind,
            p,
            'ok',
            0.0,
            self._cost_usd,
            'fake',
            level=level,
        )


class DaviRunPodVerifier(Verifier):
    """Davi through the RunPod choice endpoint (the same client, polling,
    cancel and cost estimate as the context filter). Binary kinds only."""

    judge_name = 'davi'

    def __init__(self, backend, **kwargs):
        super().__init__(**kwargs)
        self._backend = backend
        self._scrubber = backend.scrub

    def _semantic_input(self, task, candidate, rubric, kind):
        if kind == 'tier':
            raise ValueError(
                'kind tier needs a /v1/systemone judge (DAVI_BASE_URL); '
                'the RunPod choice endpoint is binary'
            )
        question = KINDS[kind]
        return {
            'turn': 1,
            'instructions': question['instructions'],
            'state': state_of(task, candidate, rubric, self.max_chars),
            'criteria': {
                'opt_accept': question['accept'],
                'opt_escalate': question['escalate'],
            },
            'question_id': QUESTION_ID,
        }

    def _verify(self, task, candidate, rubric, kind, started):
        semantic = self._semantic_input(task, candidate, rubric, kind)
        reply = self._backend.call(semantic, dict(_RUNPOD_LABELS))
        probabilities = reply.get('probabilities')
        p = None
        if isinstance(probabilities, dict) and reply.get('outcome') in (
            'ok',
            'no_decision',
        ):
            p = probabilities.get('accept')
        return Verdict(
            self.name,
            kind,
            float(p) if p is not None else None,
            'ok' if p is not None else reply.get('outcome'),
            self._elapsed(started),
            self._cost(reply),
            reply.get('response_model'),
            reply.get('usage'),
            reply.get('detail'),
        )

    def _cost(self, reply):
        if not reply.get('submissions'):
            return 0.0
        executed_ms = (reply.get('server_timing') or {}).get('execution_ms')
        rate = self._backend.usd_per_second
        if isinstance(executed_ms, (int, float)) and rate:
            return round(executed_ms / 1000.0 * rate, 8)
        return None

    def close(self):
        self._backend.close()


class HttpVerifier(Verifier):
    """Shared plumbing for JSON-over-HTTP judges: a lazily created client
    (the key is read at the first request), scrubbing and pricing."""

    default_prices = None  # (USD per M input, per M output); None = unknown

    def __init__(
        self,
        key_provider,
        base_url,
        *,
        transport=None,
        timeout=60.0,
        usd_in_per_mtok=None,
        usd_out_per_mtok=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._key_provider = key_provider
        self._base_url = base_url.rstrip('/')
        self._transport = transport
        self._timeout = timeout
        self._client = None
        self._prices = self._resolve_prices(usd_in_per_mtok, usd_out_per_mtok)

    @property
    def base_url(self):
        return self._base_url

    def _resolve_prices(self, usd_in, usd_out):
        default_in, default_out = self.default_prices or (None, 0.0)
        price_in = default_in if usd_in is None else usd_in
        price_out = default_out if usd_out is None else usd_out
        return None if price_in is None else (price_in, price_out)

    @abstractmethod
    def _headers(self, key):
        """Request headers for ``key`` (which may be None)."""

    def _http(self):
        if self._client is None:
            key = self._key_provider()
            self._scrubber.set_secret(key)
            transport = self._transport
            if transport is None:
                transport = httpx.HTTPTransport(retries=0)
            self._client = httpx.Client(
                transport=transport,
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=False,
                headers=self._headers(key),
            )
        return self._client

    def _post(self, path, body):
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        response = self._http().post(self._base_url + path, content=payload)
        try:
            data = response.json()
        except ValueError:
            data = None
        return response.status_code, data, response.text

    def _price(self, usage):
        if self._prices is None or not isinstance(usage, dict):
            return None
        tokens_in = usage.get('input_tokens')
        tokens_out = usage.get('output_tokens')
        if not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
            return None
        price_in, price_out = self._prices
        return round(
            tokens_in * price_in / 1e6 + tokens_out * price_out / 1e6, 10
        )

    def _http_failure(self, kind, started, status, text):
        return Verdict(
            self.name,
            kind,
            None,
            'http',
            self._elapsed(started),
            None,
            detail=f'HTTP {status}: {text[:200]}',
        )

    def close(self):
        if self._client is not None:
            self._client.close()


class SystemOneVerifier(HttpVerifier):
    """A judge behind ``POST /v1/systemone``: choice questions answered with
    real probabilities (Jev and Davi share this request shape)."""

    path = '/v1/systemone'

    def __init__(
        self,
        key_provider,
        base_url,
        *,
        model,
        pre_ensemble=False,
        **kwargs,
    ):
        super().__init__(key_provider, base_url, **kwargs)
        self._model = model
        self._pre_ensemble = pre_ensemble

    @property
    def model(self):
        return self._model

    def _headers(self, key):
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        if key:  # a self-served endpoint may need no key
            headers['Authorization'] = f'Bearer {key}'
        return headers

    def _uses_ensemble(self, kind):
        return kind == 'pre' and self._pre_ensemble

    def body(self, task, candidate, rubric, kind):
        """The request body for one judgment."""
        state = state_of(task, candidate, rubric, self.max_chars)
        return {
            'state': state,
            'model': self._model,
            'questions': self._questions(kind),
        }

    def _questions(self, kind):
        if self._uses_ensemble(kind):
            return {
                f'{QUESTION_ID}_{name}': {
                    'type': 'choice',
                    'instructions': instructions,
                    'criteria': criteria,
                }
                for name, (instructions, criteria, _) in PRE_ENSEMBLE.items()
            }
        question = KINDS[kind]
        if kind == 'tier':
            criteria = dict(TIER_LEVELS)
        else:
            criteria = {
                'accept': question['accept'],
                'escalate': question['escalate'],
            }
        return {
            QUESTION_ID: {
                'type': 'choice',
                'instructions': question['instructions'],
                'criteria': criteria,
            }
        }

    def _verify(self, task, candidate, rubric, kind, started):
        body = self.body(task, candidate, rubric, kind)
        status, data, text = self._post(self.path, body)
        if status != 200 or not isinstance(data, dict):
            return self._http_failure(kind, started, status, text)
        usage = (
            data.get('usage') if isinstance(data.get('usage'), dict) else None
        )
        answers = data.get('answers') or {}
        reading = self._read(kind, answers)
        return Verdict(
            self.name,
            kind,
            reading['p'],
            'ok' if reading['p'] is not None else 'format_error',
            self._elapsed(started),
            self._price(usage),
            data.get('model'),
            usage,
            reading['detail'],
            level=reading['level'],
        )

    def _read(self, kind, answers):
        """{'p', 'level', 'detail'} from the answers of one response."""
        if self._uses_ensemble(kind):
            return self._read_ensemble(answers)
        answer = answers.get(QUESTION_ID)
        probabilities = (
            answer.get('probabilities') if isinstance(answer, dict) else None
        )
        if kind == 'tier':
            level = tier_level(probabilities)
            if level is None:
                return self._unread(
                    f'no level probabilities in answers.{QUESTION_ID}'
                )
            return {'p': 1.0 - level, 'level': level, 'detail': None}
        p = (
            probabilities.get('accept')
            if isinstance(probabilities, dict)
            else None
        )
        if not _is_probability(p):
            return self._unread(
                f'no probabilities.accept in answers.{QUESTION_ID}'
            )
        return {'p': min(float(p), 1.0), 'level': None, 'detail': None}

    def _read_ensemble(self, answers):
        values = []
        for name, (_, _, accept_option) in PRE_ENSEMBLE.items():
            answer = answers.get(f'{QUESTION_ID}_{name}')
            probabilities = (
                answer.get('probabilities')
                if isinstance(answer, dict)
                else None
            )
            p = (probabilities or {}).get(accept_option)
            if not isinstance(p, (int, float)) or isinstance(p, bool):
                return self._unread(
                    'a pre-ensemble answer is missing its probabilities'
                )
            values.append(p)
        p = mean_logit_probability(values)
        return {'p': p, 'level': None, 'detail': None}

    @staticmethod
    def _unread(detail):
        return {'p': None, 'level': None, 'detail': detail}


class JevVerifier(SystemOneVerifier):
    """TypeSafe Jev. Prices default to an ASSUMED $0.042 per million input
    tokens unless configured."""

    judge_name = 'jev'
    default_prices = JEV_PRICES

    def __init__(
        self,
        key_provider,
        model=JEV_DEFAULT_MODEL,
        base_url=JEV_BASE_URL,
        **kwargs,
    ):
        super().__init__(key_provider, base_url, model=model, **kwargs)


class DaviVerifier(SystemOneVerifier):
    """Davi served at ``DAVI_BASE_URL``. The key is optional, the ``pre``
    phrasing ensemble is on by default, and the cost is unknown unless
    per-token prices are configured (self-hosted: no list price)."""

    judge_name = 'davi'
    default_prices = None

    def __init__(
        self,
        base_url,
        key_provider=None,
        model='davi',
        pre_ensemble=True,
        **kwargs,
    ):
        super().__init__(
            key_provider or (lambda: None),
            base_url,
            model=model,
            pre_ensemble=pre_ensemble,
            **kwargs,
        )


class ClaudeVerifier(HttpVerifier):
    """Anthropic Messages. The API returns no token probabilities, so the
    model answers with a number: a verbalized, coarser probability."""

    judge_name = 'claude'

    def __init__(
        self,
        key_provider,
        model=CLAUDE_DEFAULT_MODEL,
        base_url=CLAUDE_BASE_URL,
        max_tokens=None,
        effort=CLAUDE_DEFAULT_EFFORT,
        **kwargs,
    ):
        self._model = model
        super().__init__(key_provider, base_url, **kwargs)
        self._effort = effort
        # thinking models need room for the hidden reasoning first
        self._max_tokens = max_tokens or (2048 if uses_effort(model) else 8)

    @property
    def model(self):
        return self._model

    @property
    def default_prices(self):
        for prefix, prices in CLAUDE_PRICES.items():
            if self._model.startswith(prefix):
                return prices
        return CLAUDE_FALLBACK_PRICES

    def _headers(self, key):
        return {
            'x-api-key': key,
            'anthropic-version': ANTHROPIC_VERSION,
            'content-type': 'application/json',
        }

    def text(self, task, candidate, rubric, kind):
        """The single user message sent to the model."""
        question = KINDS[kind]
        parts = [question['instructions'], '']
        state = state_of(task, candidate, rubric, self.max_chars)
        for field, value in state.items():
            parts += [f'<{field}>', value, f'</{field}>', '']
        if kind == 'tier':
            parts += ['Ratings:']
            parts += [description for _, description in TIER_LEVELS]
            parts += ['', CLAUDE_TIER_FORMAT_LINE]
        else:
            parts += [
                'Options:',
                f'accept: {question["accept"]}',
                f'escalate: {question["escalate"]}',
                '',
                CLAUDE_FORMAT_LINE,
            ]
        return '\n'.join(parts)

    def body(self, task, candidate, rubric, kind):
        """The request body for one judgment."""
        message = self.text(task, candidate, rubric, kind)
        body = {
            'model': self._model,
            'max_tokens': self._max_tokens,
            'messages': [{'role': 'user', 'content': message}],
        }
        if uses_effort(self._model):
            body['output_config'] = {'effort': self._effort}
        else:
            body['temperature'] = 0.0
        return body

    def _verify(self, task, candidate, rubric, kind, started):
        body = self.body(task, candidate, rubric, kind)
        status, data, text = self._post('/v1/messages', body)
        if status != 200 or not isinstance(data, dict):
            return self._http_failure(kind, started, status, text)
        raw_usage = data.get('usage')
        usage = None
        if isinstance(raw_usage, dict):
            usage = {
                'input_tokens': raw_usage.get('input_tokens'),
                'output_tokens': raw_usage.get('output_tokens'),
            }
        reply = ''.join(
            block.get('text', '')
            for block in data.get('content') or []
            if isinstance(block, dict)
        )
        p, level, detail = self._read(kind, reply)
        return Verdict(
            self.name,
            kind,
            p,
            'ok' if p is not None else 'format_error',
            self._elapsed(started),
            self._price(usage),
            data.get('model'),
            usage,
            detail,
            level=level,
        )

    @staticmethod
    def _read(kind, reply):
        if kind == 'tier':
            ratings = _RATING_RE.findall(reply)
            if not ratings:
                return None, None, f'no 1-5 rating in reply {reply[:40]!r}'
            level = (int(ratings[-1]) - 1) / 4
            return 1.0 - level, level, None
        p = parse_percent(reply)
        if p is None:
            return None, None, f'no 0-100 number in reply {reply[:40]!r}'
        return p, None, None


# --- construction from options ----------------------------------------------
def build_verifier(
    name,
    env,
    transport=None,
    clock=time.perf_counter,
    sleep=time.sleep,
):
    """Build judge ``name`` from environment-style options.

    Nothing is read from the network or a key store here; keys are read at
    the first request. See docs/routing.md for the options.
    """
    if name not in JUDGES:
        raise ValueError(f'judge must be one of {JUDGES}')
    common = {
        'clock': clock,
        'max_chars': int(
            opt_number(env, 'verify_max_chars', DEFAULT_MAX_CHARS)
        ),
    }
    http = {
        'transport': transport,
        'timeout': opt_number(env, 'verify_timeout_seconds', 60.0),
        **common,
    }
    if name == 'fake':
        return FakeVerifier(
            default=opt_number(env, 'fake_default', 0.5), **common
        )
    if name == 'jev':
        return JevVerifier(
            lazy_key(env, SECRET_ENVS['jev']),
            model=opt(env, 'jev_model', JEV_DEFAULT_MODEL),
            base_url=opt(env, 'jev_base_url', JEV_BASE_URL),
            usd_in_per_mtok=opt_number(env, 'jev_usd_in_per_mtok'),
            usd_out_per_mtok=opt_number(env, 'jev_usd_out_per_mtok'),
            **http,
        )
    if name == 'claude':
        return ClaudeVerifier(
            lazy_key(env, SECRET_ENVS['claude']),
            model=opt(env, 'claude_judge_model', CLAUDE_DEFAULT_MODEL),
            base_url=opt(env, 'claude_base_url', CLAUDE_BASE_URL),
            usd_in_per_mtok=opt_number(env, 'claude_usd_in_per_mtok'),
            usd_out_per_mtok=opt_number(env, 'claude_usd_out_per_mtok'),
            effort=opt(env, 'claude_judge_effort', CLAUDE_DEFAULT_EFFORT),
            **http,
        )
    base_url = unquote(opt(env, 'davi_base_url') or env.get('DAVI_BASE_URL'))
    if base_url:
        return DaviVerifier(
            base_url,
            key_provider=lazy_key(
                env, SECRET_ENVS['davi_systemone'], required=False
            ),
            model=opt(env, 'davi_model', 'davi'),
            pre_ensemble=opt_flag(env, 'davi_pre_ensemble', True),
            usd_in_per_mtok=opt_number(env, 'davi_usd_in_per_mtok'),
            usd_out_per_mtok=opt_number(env, 'davi_usd_out_per_mtok'),
            **http,
        )
    return _build_runpod_davi(env, transport, clock, sleep, http, common)


def _build_runpod_davi(env, transport, clock, sleep, http, common):
    from ..contract import AdapterConfig
    from ..core import build_backend

    config = AdapterConfig(
        transport='real' if transport is None else 'fake',
        backend='choice_endpoint',
        trace_dir='unused',
        timeout_seconds=http['timeout'],
        choice_endpoint_id=first_env(env, ENDPOINT_ENVS),
        choice_poll_interval_seconds=opt_number(
            env, 'poll_interval_seconds', 2.0
        ),
        choice_max_status_polls=int(opt_number(env, 'max_status_polls', 30)),
        choice_usd_per_second=opt_number(env, 'usd_per_second'),
    )
    backend = build_backend(
        config,
        transport=transport,
        key_provider=lazy_key(env, SECRET_ENVS['davi']),
        clock=clock,
        sleep=sleep,
    )
    return DaviRunPodVerifier(backend, **common)
