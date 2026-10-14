# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""One pointwise verifier interface for three judges.

    verify(task, candidate, rubric=None, kind='answer') -> Verdict(p_accept, outcome, latency_s, cost_usd, ...)

Judges (same question wording and the same two options for all of them):

- ``davi``   the user-served choice endpoint on RunPod (``ChoiceEndpointBackend``): one choice question, P from the
             returned probabilities. Cost = server executionTime x configured USD/s (an estimate).
- ``jev``    TypeSafe ``POST /v1/systemone`` (Bearer key), one ``choice`` question, P from ``probabilities``.
             Cost = usage x configured per-MTok prices (defaults are an ASSUMPTION, see JEV_PRICE_NOTE).
- ``claude`` Anthropic Messages API. The API returns no token probabilities, so the model is asked for a number
             0-100 (a VERBALIZED probability: coarser and usually less calibrated than the other two). Cost = usage x
             configured per-MTok prices.
- ``fake``   in-process, reads ``[[ACCEPT=p]]`` in the candidate (tests and dry runs).

Kinds: ``answer`` (can this answer be used as final, or should a stronger model redo it?) and ``completion`` (is the
agent's task actually complete, or should it continue?). In both, option ``accept`` means "keep / stop" and option
``escalate`` means "redo with a stronger model / continue".

No verifier raises: every failure is a Verdict with ``p_accept=None`` and ``outcome`` naming the reason. Keys are read
lazily at the first request and are scrubbed from every detail string.
"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from dataclasses import dataclass

import httpx

from ..keys import CredentialMissing, Scrubber

QUESTION_ID = 'route'
LABELS = ('accept', 'escalate')

KINDS = {
    'answer': {
        'instructions': (
            'A lower-cost AI model produced the answer in the field "candidate" for the task in the field "task". '
            'Decide whether this answer can be used as the final answer as it is: it must be correct, complete and '
            'follow the task\'s instructions. If the field "rubric" is present, apply it. Choose "accept" only when '
            'the answer is acceptable. Choose "escalate" when it may be wrong, incomplete, unsupported or off-task, '
            'so that a stronger model solves the task again.'),
        'accept': 'Accept: the answer is correct and complete; use it as the final answer.',
        'escalate': 'Escalate: the answer may be wrong or incomplete; a stronger model should solve the task again.',
    },
    'completion': {
        'instructions': (
            'An AI agent is about to stop working on the user request in the field "task". Its final message is in '
            'the field "candidate". Decide whether the requested task is actually complete: the requested final '
            'state is reached, no requested step is left undone, and the agent did not stop early or claim success '
            'without doing the work. If the field "rubric" is present, apply it. Choose "accept" only when the task '
            'is complete. Choose "escalate" when it is not complete or completion is not shown.'),
        'accept': 'Complete: the requested task is done; the agent can stop.',
        'escalate': 'Not complete: something requested is missing or unverified; the agent should continue.',
    },
}

DEFAULT_MAX_CHARS = 24_000
JEV_DEFAULT_MODEL = 'jev-latest'
JEV_BASE_URL = 'https://api.typesafe.ai'
JEV_PRICE_IN_PER_MTOK = 0.042          # ASSUMPTION (the "Jev-style list price" used in this repo's README); set yours
JEV_PRICE_OUT_PER_MTOK = 0.0
JEV_PRICE_NOTE = 'ASSUMED Jev prices ($0.042 in / $0 out per MTok) unless configured; not a bill'
CLAUDE_DEFAULT_MODEL = 'claude-haiku-4-5-20251001'
CLAUDE_BASE_URL = 'https://api.anthropic.com'
CLAUDE_PRICE_IN_PER_MTOK = 1.00        # list price of the default model as checked 2026-09; set yours for another model
CLAUDE_PRICE_OUT_PER_MTOK = 5.00
ANTHROPIC_VERSION = '2023-06-01'
CLAUDE_FORMAT_LINE = ('Reply with only an integer from 0 to 100: your probability, in percent, that "accept" is the '
                      'right option. No other text.')


@dataclass
class Verdict:
    judge: str
    kind: str
    p_accept: float | None             # P(accept); None on failure
    outcome: str                       # 'ok' or a failure reason
    latency_s: float
    cost_usd: float | None             # estimate; None = unknown (never silently 0 for a sent request)
    model: str | None = None
    usage: dict | None = None
    detail: str | None = None

    def to_dict(self):
        return dataclasses.asdict(self)


def clip(text, max_chars=DEFAULT_MAX_CHARS):
    """Keep the head and the tail of a long text (agent outputs end with the conclusion)."""
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head
    return f'{text[:head]}\n[... {len(text) - max_chars} characters omitted ...]\n{text[-tail:]}'


def state_of(task, candidate, rubric=None, max_chars=DEFAULT_MAX_CHARS):
    st = {'task': clip(task or '', max_chars), 'candidate': clip(candidate or '', max_chars)}
    if rubric:
        st['rubric'] = clip(rubric, max_chars)
    return st


def semantic_input(kind, task, candidate, rubric=None, max_chars=DEFAULT_MAX_CHARS):
    """The choice-endpoint semantic input (same shape ``ChoiceEndpointBackend`` sends for the context filter)."""
    k = KINDS[kind]
    return {'turn': 1, 'instructions': k['instructions'], 'state': state_of(task, candidate, rubric, max_chars),
            'criteria': {'opt_accept': k['accept'], 'opt_escalate': k['escalate']}, 'question_id': QUESTION_ID}


TO_LABEL = {'opt_accept': 'accept', 'opt_escalate': 'escalate'}


class _Base:
    name = 'base'

    def __init__(self, clock=time.perf_counter, max_chars=DEFAULT_MAX_CHARS):
        self.clock = clock
        self.max_chars = max_chars
        self.scrub = Scrubber()

    def verify(self, task, candidate, rubric=None, kind='answer'):
        if kind not in KINDS:
            raise ValueError(f'kind must be one of {sorted(KINDS)}')
        t0 = self.clock()
        try:
            v = self._verify(task, candidate, rubric, kind, t0)
        except CredentialMissing as e:
            v = Verdict(self.name, kind, None, 'local_config', self.clock() - t0, 0.0, detail=self.scrub.text(e))
        except httpx.TimeoutException as e:
            v = Verdict(self.name, kind, None, 'timeout', self.clock() - t0, None, detail=type(e).__name__)
        except Exception as e:  # noqa: BLE001 - a verifier never raises
            v = Verdict(self.name, kind, None, 'error', self.clock() - t0, None,
                        detail=self.scrub.text(f'{type(e).__name__}: {str(e)[:200]}'))
        if v.detail:
            v.detail = self.scrub.text(v.detail)
        return v

    def _verify(self, task, candidate, rubric, kind, t0):
        raise NotImplementedError

    def close(self):
        pass


class DaviVerifier(_Base):
    """The user-served choice endpoint through the existing ``ChoiceEndpointBackend`` (same client, polling, cancel and
    cost estimate as the context filter)."""
    name = 'davi'

    def __init__(self, backend, **kw):
        super().__init__(**kw)
        self.backend = backend
        self.scrub = backend.scrub

    def _verify(self, task, candidate, rubric, kind, t0):
        sem = semantic_input(kind, task, candidate, rubric, self.max_chars)
        r = self.backend.call(sem, dict(TO_LABEL))
        probs = r.get('probabilities')
        p = probs.get('accept') if isinstance(probs, dict) and r.get('outcome') in ('ok', 'no_decision') else None
        ex = (r.get('server_timing') or {}).get('execution_ms')
        if not r.get('submissions'):
            cost = 0.0
        elif isinstance(ex, (int, float)) and self.backend.usd_per_second:
            cost = round(ex / 1000.0 * self.backend.usd_per_second, 8)
        else:
            cost = None
        return Verdict(self.name, kind, float(p) if p is not None else None, 'ok' if p is not None else r.get('outcome'),
                       self.clock() - t0, cost, r.get('response_model'), r.get('usage'), r.get('detail'))

    def close(self):
        self.backend.close()


class _HttpJudge(_Base):
    def __init__(self, key_provider, base_url, transport=None, timeout=60.0, **kw):
        super().__init__(**kw)
        self._key_provider = key_provider
        self.base_url = base_url.rstrip('/')
        self._transport = transport
        self._timeout = timeout
        self._http = None

    def _headers(self, key):
        raise NotImplementedError

    def _client(self):
        if self._http is None:
            key = self._key_provider()
            self.scrub.set_secret(key)
            tr = self._transport if self._transport is not None else httpx.HTTPTransport(retries=0)
            self._http = httpx.Client(transport=tr, timeout=httpx.Timeout(self._timeout), follow_redirects=False,
                                      headers=self._headers(key))
        return self._http

    def _post(self, path, body):
        r = self._client().post(self.base_url + path, content=json.dumps(body, ensure_ascii=False).encode('utf-8'))
        try:
            js = r.json()
        except ValueError:
            js = None
        return r.status_code, js, r.text

    @staticmethod
    def _price(usage, p_in, p_out):
        if not isinstance(usage, dict):
            return None
        it, ot = usage.get('input_tokens'), usage.get('output_tokens')
        if not isinstance(it, int) or not isinstance(ot, int):
            return None
        return round(it * p_in / 1e6 + ot * p_out / 1e6, 10)

    def close(self):
        if self._http is not None:
            self._http.close()


class JevVerifier(_HttpJudge):
    name = 'jev'

    def __init__(self, key_provider, model=JEV_DEFAULT_MODEL, base_url=JEV_BASE_URL, usd_in_per_mtok=None,
                 usd_out_per_mtok=None, **kw):
        super().__init__(key_provider, base_url, **kw)
        self.model = model
        self.p_in = JEV_PRICE_IN_PER_MTOK if usd_in_per_mtok is None else usd_in_per_mtok
        self.p_out = JEV_PRICE_OUT_PER_MTOK if usd_out_per_mtok is None else usd_out_per_mtok

    def _headers(self, key):
        return {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'Accept': 'application/json'}

    def body(self, task, candidate, rubric, kind):
        k = KINDS[kind]
        return {'state': state_of(task, candidate, rubric, self.max_chars), 'model': self.model,
                'questions': {QUESTION_ID: {'type': 'choice', 'instructions': k['instructions'],
                                            'criteria': {'accept': k['accept'], 'escalate': k['escalate']}}}}

    def _verify(self, task, candidate, rubric, kind, t0):
        status, js, text = self._post('/v1/systemone', self.body(task, candidate, rubric, kind))
        lat = self.clock() - t0
        if status != 200 or not isinstance(js, dict):
            return Verdict(self.name, kind, None, 'http', lat, None, detail=f'HTTP {status}: {text[:200]}')
        usage = js.get('usage') if isinstance(js.get('usage'), dict) else None
        cost = self._price(usage, self.p_in, self.p_out)
        ans = (js.get('answers') or {}).get(QUESTION_ID)
        probs = ans.get('probabilities') if isinstance(ans, dict) else None
        p = probs.get('accept') if isinstance(probs, dict) else None
        if not isinstance(p, (int, float)) or isinstance(p, bool) or not 0.0 <= p <= 1.0 + 1e-9:
            return Verdict(self.name, kind, None, 'format_error', lat, cost, js.get('model'), usage,
                           f'no probabilities.accept in answers.{QUESTION_ID}')
        return Verdict(self.name, kind, min(float(p), 1.0), 'ok', lat, cost, js.get('model'), usage)


_INT_RE = re.compile(r'\d{1,3}(?:\.\d+)?')


def parse_percent(text):
    m = _INT_RE.search(text or '')
    if not m:
        return None
    v = float(m.group(0))
    return v / 100.0 if 0.0 <= v <= 100.0 else None


class ClaudeVerifier(_HttpJudge):
    name = 'claude'

    def __init__(self, key_provider, model=CLAUDE_DEFAULT_MODEL, base_url=CLAUDE_BASE_URL, usd_in_per_mtok=None,
                 usd_out_per_mtok=None, max_tokens=8, **kw):
        super().__init__(key_provider, base_url, **kw)
        self.model = model
        self.max_tokens = max_tokens
        self.p_in = CLAUDE_PRICE_IN_PER_MTOK if usd_in_per_mtok is None else usd_in_per_mtok
        self.p_out = CLAUDE_PRICE_OUT_PER_MTOK if usd_out_per_mtok is None else usd_out_per_mtok

    def _headers(self, key):
        return {'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION, 'content-type': 'application/json'}

    def text(self, task, candidate, rubric, kind):
        k = KINDS[kind]
        parts = [k['instructions'], '']
        for f, v in state_of(task, candidate, rubric, self.max_chars).items():
            parts += [f'<{f}>', v, f'</{f}>', '']
        parts += ['Options:', f'accept: {k["accept"]}', f'escalate: {k["escalate"]}', '', CLAUDE_FORMAT_LINE]
        return '\n'.join(parts)

    def body(self, task, candidate, rubric, kind):
        return {'model': self.model, 'max_tokens': self.max_tokens, 'temperature': 0.0,
                'messages': [{'role': 'user', 'content': self.text(task, candidate, rubric, kind)}]}

    def _verify(self, task, candidate, rubric, kind, t0):
        status, js, text = self._post('/v1/messages', self.body(task, candidate, rubric, kind))
        lat = self.clock() - t0
        if status != 200 or not isinstance(js, dict):
            return Verdict(self.name, kind, None, 'http', lat, None, detail=f'HTTP {status}: {text[:200]}')
        u = js.get('usage') if isinstance(js.get('usage'), dict) else None
        usage = {'input_tokens': u.get('input_tokens'), 'output_tokens': u.get('output_tokens')} if u else None
        cost = self._price(usage, self.p_in, self.p_out)
        out = ''.join(b.get('text', '') for b in js.get('content') or [] if isinstance(b, dict))
        p = parse_percent(out)
        if p is None:
            return Verdict(self.name, kind, None, 'format_error', lat, cost, js.get('model'), usage,
                           f'no 0-100 number in reply {out[:40]!r}')
        return Verdict(self.name, kind, p, 'ok', lat, cost, js.get('model'), usage)


_ACCEPT_RE = re.compile(r'\[\[ACCEPT=([0-9.]+)\]\]')


class FakeVerifier(_Base):
    """In-process judge for tests and dry runs: ``[[ACCEPT=p]]`` in the candidate sets P(accept); ``FAKE:ERROR``
    fails; no marker -> ``default``. Cost ``cost_usd`` per call, latency 0. Not a model."""
    name = 'fake'

    def __init__(self, default=0.5, cost_usd=0.0, name='fake', **kw):
        super().__init__(**kw)
        self.default = default
        self.cost_usd = cost_usd
        self.name = name
        self.calls = []

    def _verify(self, task, candidate, rubric, kind, t0):
        self.calls.append({'task': task, 'candidate': candidate, 'rubric': rubric, 'kind': kind})
        if 'FAKE:ERROR' in (candidate or ''):
            return Verdict(self.name, kind, None, 'error', 0.0, None, detail='fake error')
        m = _ACCEPT_RE.search(candidate or '')
        p = float(m.group(1)) if m else self.default
        return Verdict(self.name, kind, p, 'ok', 0.0, self.cost_usd, 'fake')


# --- configuration -----------------------------------------------------------------------------------------------------
JUDGES = ('davi', 'jev', 'claude', 'fake')
OVERRIDE_PREFIX = 'DAVI_ROUTE_'                 # explicit per-run override (harness, shell); wins over userConfig
OPTION_PREFIX = 'CLAUDE_PLUGIN_OPTION_'         # Claude Code exports userConfig as this + KEY.upper()
SECRET_ENVS = {
    'davi': ('DAVI_ROUTE_RUNPOD_API_KEY', 'CLAUDE_PLUGIN_OPTION_RUNPOD_API_KEY', 'RUNPOD_API_KEY'),
    'jev': ('DAVI_ROUTE_TYPESAFE_API_KEY', 'CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY', 'TYPESAFE_API_KEY'),
    'claude': ('DAVI_ROUTE_ANTHROPIC_API_KEY', 'CLAUDE_PLUGIN_OPTION_ANTHROPIC_API_KEY', 'ANTHROPIC_API_KEY'),
}
ENDPOINT_ENVS = ('DAVI_ROUTE_CHOICE_ENDPOINT_ID', 'CLAUDE_PLUGIN_OPTION_CHOICE_ENDPOINT_ID',
                 'JUDGE_ADAPTER_CHOICE_ENDPOINT_ID')


def opt(env, key, default=None):
    """DAVI_ROUTE_<KEY>, then CLAUDE_PLUGIN_OPTION_<KEY>, then ``default``. Empty strings count as unset."""
    for p in (OVERRIDE_PREFIX, OPTION_PREFIX):
        v = env.get(p + key.upper())
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def _fnum(env, key, default=None):
    v = opt(env, key)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        raise ValueError(f'{key} must be a number') from None


def lazy_key(env, names):
    def provider():
        for n in names:
            v = env.get(n, '')
            if isinstance(v, str) and v.strip():
                return v.strip()
        raise CredentialMissing(f'none of {", ".join(names)} is set')
    return provider


def build_verifier(name, env, transport=None, clock=time.perf_counter, sleep=time.sleep):
    """Build a judge from environment-style options (see docs/routing.md). Nothing is read from the network or the
    key store here; keys are read at the first request."""
    if name not in JUDGES:
        raise ValueError(f'judge must be one of {JUDGES}')
    max_chars = int(_fnum(env, 'verify_max_chars', DEFAULT_MAX_CHARS))
    timeout = _fnum(env, 'verify_timeout_seconds', 60.0)
    if name == 'fake':
        return FakeVerifier(default=_fnum(env, 'fake_default', 0.5), max_chars=max_chars, clock=clock)
    if name == 'jev':
        return JevVerifier(lazy_key(env, SECRET_ENVS['jev']), model=opt(env, 'jev_model', JEV_DEFAULT_MODEL),
                           base_url=opt(env, 'jev_base_url', JEV_BASE_URL),
                           usd_in_per_mtok=_fnum(env, 'jev_usd_in_per_mtok'),
                           usd_out_per_mtok=_fnum(env, 'jev_usd_out_per_mtok'), transport=transport, timeout=timeout,
                           max_chars=max_chars, clock=clock)
    if name == 'claude':
        return ClaudeVerifier(lazy_key(env, SECRET_ENVS['claude']),
                              model=opt(env, 'claude_judge_model', CLAUDE_DEFAULT_MODEL),
                              base_url=opt(env, 'claude_base_url', CLAUDE_BASE_URL),
                              usd_in_per_mtok=_fnum(env, 'claude_usd_in_per_mtok'),
                              usd_out_per_mtok=_fnum(env, 'claude_usd_out_per_mtok'), transport=transport,
                              timeout=timeout, max_chars=max_chars, clock=clock)
    # davi: the existing choice-endpoint backend
    from ..contract import AdapterConfig
    from ..core import build_backend
    endpoint = None
    for n in ENDPOINT_ENVS:
        if env.get(n, '').strip():
            endpoint = env[n].strip()
            break
    cfg = AdapterConfig(transport='real' if transport is None else 'fake', backend='choice_endpoint',
                        trace_dir='unused', timeout_seconds=timeout, choice_endpoint_id=endpoint,
                        choice_poll_interval_seconds=_fnum(env, 'poll_interval_seconds', 2.0),
                        choice_max_status_polls=int(_fnum(env, 'max_status_polls', 30)),
                        choice_usd_per_second=_fnum(env, 'usd_per_second'))
    backend = build_backend(cfg, transport=transport, key_provider=lazy_key(env, SECRET_ENVS['davi']), clock=clock,
                            sleep=sleep)
    return DaviVerifier(backend, max_chars=max_chars, clock=clock)
