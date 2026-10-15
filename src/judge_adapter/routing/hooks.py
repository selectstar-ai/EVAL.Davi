# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Claude Code hook logic for routing experiments (loaded by ``plugins/davi-route/hooks/route_hook.py``).

Three hooks, all default-off, fail-open (any error leaves Claude Code's behaviour unchanged), one JSON log line each.
``route_mode`` picks how A and E route subagents (``route_enabled`` turns both on or off):

- ``post`` (default)  cheap model first (E pins it), A verifies the answer and asks for a strong re-run when needed.
- ``pre``             E judges the task alone before the subagent runs and sends it to the cheap or the strong model
                      directly; A does not verify. Nothing runs twice, but the judge never sees an answer.
- ``both``            E routes up front as in ``pre``; a task sent to the cheap model is then verified by A as in
                      ``post``. A task already sent to the strong model is not verified again.
- ``tiers``           E rates the task's difficulty for a small model (kind ``tier``, level in [0, 1]) and picks one
                      of three models: below ``route_tier_low`` the cheap model, from ``route_tier_high`` the strong
                      model, the mid model (``route_mid_model``) in between. With ``route_tier_check`` A verifies
                      what went to the cheap model as in ``post``; otherwise nothing is verified.

A  ``post``  PostToolUse on the Agent tool. The verifier reads the subagent's task (``tool_input.prompt``) and result
             (``tool_response``) and returns P(accept). Below the threshold the hook (1) tells the main agent to re-run
             the task with the strong model (``additionalContext``, or ``decision: block`` + reason in ``block`` mode)
             and (2) stores the strong model as the session's pending next model (for E).
E  ``pre``   PreToolUse on the Agent tool. Sets ``tool_input.model`` through ``updatedInput``: the pending next model
             stored by A if there is one (one-shot); else, in ``pre``/``both`` mode, the model a task-only verdict
             (kind ``pre``, threshold ``route_pre_threshold``) picks; else the cheap model (``route_pin_cheap``). This
             is how "the next model as a string" reaches Claude Code: per Agent call, not for the main session (no
             hook can switch the main session's model).
B  ``stop``  Stop (and, if enabled, SubagentStop). The verifier reads the user's request (last user prompt in the
             transcript) and the final assistant message and returns P(complete). Below the threshold it blocks the
             stop once (``decision: block`` + reason), at most ``stop_max_blocks`` times per request.

Probe mode (``DAVI_ROUTE_PROBE=1``) makes no verifier call. It checks the mechanisms themselves:
  post  adds a marker (DAVI-PROBE-A-<nonce>) to additionalContext and logs the payload shape;
  pre   rewrites the Agent model to ``probe_pre_model`` if set, and logs the requested model;
  stop  blocks once with a reason that asks for DAVI-PROBE-B-<nonce>, then allows (loop guard check).
``davi-route probe`` runs these and reads the results.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field

from . import verifiers as V
from .policy import RoutePolicy, same_model

AGENT_TOOLS = ('Agent', 'Task')
LOG_EVENT = 'davi-route'
STATE_DIRNAME = 'route-state'
PENDING_TTL_SECONDS = 1800
TRUE_WORDS, FALSE_WORDS = ('1', 'true', 'yes', 'on'), ('0', 'false', 'no', 'off')
PIN_MODES = ('always', 'if_unset', 'off')
POST_MODES = ('context', 'block')
ROUTE_MODES = ('post', 'pre', 'both', 'tiers')


class ConfigError(ValueError):
    pass


def _flag(env, key, default):
    v = V.opt(env, key)
    if v is None:
        return default
    s = v.lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    raise ConfigError(f'{key} must be true or false')


def _num(env, key, default, lo, hi, cast=float):
    v = V.opt(env, key)
    if v is None:
        return default
    try:
        x = cast(float(v))
    except ValueError:
        raise ConfigError(f'{key} must be a number') from None
    if not lo <= x <= hi:
        raise ConfigError(f'{key} must be in [{lo}, {hi}]')
    return x


def _choice(env, key, default, allowed):
    v = (V.opt(env, key) or default).lower()
    if v not in allowed:
        raise ConfigError(f'{key} must be one of {allowed}')
    return v


@dataclass(frozen=True)
class Settings:
    route_enabled: bool = False
    route_mode: str = 'post'
    route_judge: str = 'davi'
    route_threshold: float = 0.5
    route_pre_threshold: float = 0.5
    route_cheap_model: str = 'haiku'
    route_strong_model: str = 'opus'
    route_mid_model: str = 'sonnet'
    route_tier_low: float = 0.48            # KLEAR-fitted (haiku 5.5 / sonnet 5.5 / opus 5.5); recalibrate per workload
    route_tier_high: float = 0.78
    route_tier_check: bool = False
    route_pin_cheap: str = 'always'
    route_post_mode: str = 'context'
    route_agent_types: tuple = ()           # empty = every subagent type
    stop_verify_enabled: bool = False
    stop_judge: str = ''                    # empty = route_judge
    stop_threshold: float = 0.5
    stop_max_blocks: int = 1
    stop_subagents: bool = False
    on_error: str = 'accept'
    wall_budget_seconds: float = 25.0
    log_file: str = ''
    probe: bool = False
    probe_nonce: str = 'X'
    probe_pre_model: str = ''
    probe_stop: bool = True                 # probe mode only: False skips the Stop probe (agent / pre probe runs)
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.route_tier_low > self.route_tier_high:
            raise ConfigError('route_tier_low must not be above route_tier_high')

    @property
    def policy(self):
        return RoutePolicy(self.route_threshold, self.route_strong_model, self.route_cheap_model, self.on_error)

    @property
    def pre_policy(self):
        return RoutePolicy(self.route_pre_threshold, self.route_strong_model, self.route_cheap_model, self.on_error)


def load_settings(env):
    types = tuple(t.strip() for t in (V.opt(env, 'route_agent_types') or '').split(',') if t.strip())
    judges = V.JUDGES
    return Settings(
        route_enabled=_flag(env, 'route_enabled', False),
        route_mode=_choice(env, 'route_mode', 'post', ROUTE_MODES),
        route_judge=_choice(env, 'route_judge', 'davi', judges),
        route_threshold=_num(env, 'route_threshold', 0.5, 0.0, 1.0),
        route_pre_threshold=_num(env, 'route_pre_threshold', 0.5, 0.0, 1.0),
        route_cheap_model=V.opt(env, 'route_cheap_model', 'haiku'),
        route_strong_model=V.opt(env, 'route_strong_model', 'opus'),
        route_mid_model=V.opt(env, 'route_mid_model', 'sonnet'),
        route_tier_low=_num(env, 'route_tier_low', 0.48, 0.0, 1.0),
        route_tier_high=_num(env, 'route_tier_high', 0.78, 0.0, 1.0),
        route_tier_check=_flag(env, 'route_tier_check', False),
        route_pin_cheap=_choice(env, 'route_pin_cheap', 'always', PIN_MODES),
        route_post_mode=_choice(env, 'route_post_mode', 'context', POST_MODES),
        route_agent_types=types,
        stop_verify_enabled=_flag(env, 'stop_verify_enabled', False),
        stop_judge=_choice(env, 'stop_judge', V.opt(env, 'route_judge', 'davi'), judges),
        stop_threshold=_num(env, 'stop_threshold', 0.5, 0.0, 1.0),
        stop_max_blocks=_num(env, 'stop_max_blocks', 1, 0, 5, int),
        stop_subagents=_flag(env, 'stop_subagents', False),
        on_error=_choice(env, 'on_error', 'accept', ('accept', 'escalate')),
        wall_budget_seconds=_num(env, 'wall_budget_seconds', 25.0, 1.0, 120.0),
        log_file=V.opt(env, 'log_file', ''),
        probe=_flag(env, 'probe', False),
        probe_nonce=V.opt(env, 'probe_nonce', 'X'),
        probe_pre_model=V.opt(env, 'probe_pre_model', ''),
        probe_stop=_flag(env, 'probe_stop', True),
    )


# --- local state ---------------------------------------------------------------------------------------------------
def _h(s, n=24):
    return hashlib.sha256((s or '').encode('utf-8')).hexdigest()[:n]


def state_dir(env):
    o = env.get('DAVI_ROUTE_STATE_DIR')
    if isinstance(o, str) and o.strip():
        return o.strip()
    d = env.get('CLAUDE_PLUGIN_DATA')
    if isinstance(d, str) and d.strip():
        return os.path.join(d, STATE_DIRNAME)
    import tempfile
    uid = os.getuid() if hasattr(os, 'getuid') else 'user'
    return os.path.join(tempfile.gettempdir(), f'davi-route-{STATE_DIRNAME}-{uid}')


class State:
    """Tiny per-session JSON files (atomic replace). Pending next model (one-shot, TTL) and Stop block counters."""

    def __init__(self, root, now=time.time):
        self.root = root
        self.now = now

    def _path(self, kind, key):
        return os.path.join(self.root, kind, _h(key) + '.json')

    def _read(self, kind, key):
        try:
            with open(self._path(kind, key), encoding='utf-8') as f:
                v = json.load(f)
            return v if isinstance(v, dict) else None
        except (OSError, ValueError):
            return None

    def _write(self, kind, key, value):
        p = self._path(kind, key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f'{p}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(value, f)
        os.replace(tmp, p)

    def set_pending(self, session, model, reason):
        self._write('pending', session, {'model': model, 'reason': reason, 'at': self.now()})

    def take_pending(self, session):
        v = self._read('pending', session)
        if not v:
            return None
        try:
            os.remove(self._path('pending', session))
        except OSError:
            pass
        if self.now() - float(v.get('at', 0)) > PENDING_TTL_SECONDS or not isinstance(v.get('model'), str):
            return None
        return v

    def blocks(self, key):
        v = self._read('stop', key) or {}
        return int(v.get('blocks', 0))

    def add_block(self, key):
        self._write('stop', key, {'blocks': self.blocks(key) + 1, 'at': self.now()})

    def reset_blocks(self, key):
        try:
            os.remove(self._path('stop', key))
        except OSError:
            pass


# --- payload helpers -------------------------------------------------------------------------------------------------
def response_text(resp):
    """Text of a tool response of unknown shape: string, {content: [{type: text, text}]}, list of blocks, ..."""
    if isinstance(resp, str):
        return resp
    out = []

    def walk(o, depth=0):
        if depth > 6:
            return
        if isinstance(o, dict):
            if o.get('type') == 'text' and isinstance(o.get('text'), str):
                out.append(o['text'])
                return
            for k in ('content', 'result', 'output', 'text', 'message'):
                if k in o:
                    walk(o[k], depth + 1)
        elif isinstance(o, list):
            for x in o:
                walk(x, depth + 1)
        elif isinstance(o, str) and depth > 0:
            out.append(o)
    walk(resp)
    return '\n'.join(out) if out else json.dumps(resp, ensure_ascii=False, default=str)


def shape(o, depth=0):
    """Keys and types only (no values) for probe logs."""
    if depth > 3:
        return type(o).__name__
    if isinstance(o, dict):
        return {k: shape(v, depth + 1) for k, v in list(o.items())[:30]}
    if isinstance(o, list):
        return [shape(o[0], depth + 1)] if o else []
    return type(o).__name__


def _user_text(content):
    """User message text, or None for a tool-result message."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
            return None
        t = [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text']
        return '\n'.join(t) if t else None
    return None


def transcript_messages(path, max_bytes=20_000_000):
    """(last user prompt text, last assistant text) from a Claude Code transcript JSONL. Missing file -> (None, None)."""
    if not path or not os.path.isfile(path):
        return None, None
    user = assistant = None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            size = os.path.getsize(path)
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = o.get('message') if isinstance(o, dict) else None
                if not isinstance(msg, dict):
                    continue
                if o.get('type') == 'user' and not o.get('isMeta'):
                    t = _user_text(msg.get('content'))
                    if t and t.strip() and not t.lstrip().startswith('<'):
                        user = t
                elif o.get('type') == 'assistant':
                    t = response_text(msg.get('content'))
                    if t and t.strip():
                        assistant = t
    except OSError:
        return None, None
    return user, assistant


# --- the three hooks -------------------------------------------------------------------------------------------------
def _escalate_message(p, thr, next_model):
    return (f'[davi-route] An automated verifier rated this subagent result as likely not acceptable '
            f'(P(accept)={p if p is None else round(p, 3)} < {thr}). Do not use it as the final result. Re-run the same '
            f'task with the Agent tool, passing model "{next_model}" and the same prompt, and use that result instead.')


def process_post(payload, s, verifier, state):
    """A. -> (hook output dict or None, log record)."""
    tin = payload.get('tool_input') if isinstance(payload.get('tool_input'), dict) else {}
    model_in = tin.get('model')
    resp = payload.get('tool_response')
    rec = {'hook': 'post', 'tool': payload.get('tool_name'), 'agent_type': tin.get('subagent_type'),
           'model_in': model_in, 'resolved_model': resp.get('resolvedModel') if isinstance(resp, dict) else None}
    if s.probe:
        marker = f'DAVI-PROBE-A-{s.probe_nonce}'
        rec.update(status='probe', marker=marker, tool_input_shape=shape(tin),
                   tool_response_shape=shape(payload.get('tool_response')), payload_keys=sorted(payload))
        ctx = (f'Hook probe marker: {marker}. If you can read this line, include the marker verbatim in your final '
               'answer.')
        return {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': ctx}}, rec
    if s.route_mode == 'pre':
        return None, {**rec, 'status': 'mode_pre'}
    if s.route_mode == 'tiers':
        ran = [m for m in (model_in, rec['resolved_model']) if m]
        if not s.route_tier_check:
            return None, {**rec, 'status': 'mode_tiers'}
        if ran and not any(same_model(m, s.route_cheap_model) for m in ran):
            return None, {**rec, 'status': 'mode_tiers', 'reason': 'not_cheap'}      # mid / strong: not re-checked
    if s.route_agent_types and tin.get('subagent_type') not in s.route_agent_types:
        return None, {**rec, 'status': 'type_skipped'}
    if any(same_model(m, s.route_strong_model) for m in (model_in, rec['resolved_model']) if m):
        return None, {**rec, 'status': 'ok', 'decision': 'accept', 'reason': 'already_strong'}
    task = tin.get('prompt') or tin.get('description') or ''
    result = response_text(payload.get('tool_response'))
    rec.update(task_sha=_h(task, 12), result_chars=len(result))
    if not task.strip() or not result.strip():
        return None, {**rec, 'status': 'no_input'}
    v = verifier.verify(task, result, kind='answer')
    route = s.policy.decide(v.p_accept, current_model=model_in)
    rec.update(status='ok' if v.outcome == 'ok' else 'verifier_failed', judge=v.judge, p_accept=v.p_accept,
               outcome=v.outcome, decision=route.decision, reason=route.reason, next_model=route.next_model,
               threshold=s.route_threshold, latency_s=round(v.latency_s, 4), cost_usd=v.cost_usd,
               judge_model=v.model)
    if v.outcome != 'ok':
        rec['detail'] = v.detail          # the verifier's reason, keys already scrubbed
    if route.decision != 'escalate':
        return None, rec
    state.set_pending(payload.get('session_id', ''), route.next_model, route.reason)
    msg = _escalate_message(v.p_accept, s.route_threshold, route.next_model)
    if s.route_post_mode == 'block':
        return {'decision': 'block', 'reason': msg}, rec
    return {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': msg}}, rec


def process_pre(payload, s, state, verifier=None):
    """E. -> (hook output dict or None, log record). Calls ``verifier`` (kind ``pre``) only in ``pre``/``both`` mode
    when no escalation is pending."""
    tin = payload.get('tool_input') if isinstance(payload.get('tool_input'), dict) else {}
    requested = tin.get('model')
    rec = {'hook': 'pre', 'tool': payload.get('tool_name'), 'agent_type': tin.get('subagent_type'),
           'model_requested': requested}
    if s.probe:
        rec.update(status='probe', tool_input_shape=shape(tin), payload_keys=sorted(payload))
        if not s.probe_pre_model:
            return None, rec
        target, source = s.probe_pre_model, 'probe'
    else:
        if s.route_agent_types and tin.get('subagent_type') not in s.route_agent_types:
            return None, {**rec, 'status': 'type_skipped'}
        pending = state.take_pending(payload.get('session_id', ''))
        task = tin.get('prompt') or tin.get('description') or ''
        if pending:
            target, source = pending['model'], 'escalation'
        elif s.route_mode == 'tiers' and verifier is not None and task.strip():
            v = verifier.verify(task, kind='tier')
            lvl = v.level
            if lvl is None:                      # judge failure: fail-open to the cheap model unless on_error escalates
                tier = 'strong' if s.on_error == 'escalate' else 'cheap'
            else:
                tier = 'cheap' if lvl < s.route_tier_low else ('strong' if lvl >= s.route_tier_high else 'mid')
            target = {'cheap': s.route_cheap_model, 'mid': s.route_mid_model, 'strong': s.route_strong_model}[tier]
            source = 'tier_route'
            rec.update(judge=v.judge, level=lvl, tier=tier, outcome=v.outcome, latency_s=round(v.latency_s, 4),
                       cost_usd=v.cost_usd, judge_model=v.model, task_sha=_h(task, 12),
                       tier_low=s.route_tier_low, tier_high=s.route_tier_high,
                       status='ok' if v.outcome == 'ok' else 'verifier_failed')
            if v.outcome != 'ok':
                rec['detail'] = v.detail
        elif s.route_mode in ('pre', 'both') and verifier is not None and task.strip():
            v = verifier.verify(task, kind='pre')
            route = s.pre_policy.decide(v.p_accept)
            target, source = (route.next_model if route.decision == 'escalate' else s.route_cheap_model), 'pre_route'
            rec.update(judge=v.judge, p_accept=v.p_accept, outcome=v.outcome, decision=route.decision,
                       reason=route.reason, threshold=s.route_pre_threshold, latency_s=round(v.latency_s, 4),
                       cost_usd=v.cost_usd, judge_model=v.model, task_sha=_h(task, 12),
                       status='ok' if v.outcome == 'ok' else 'verifier_failed')
            if v.outcome != 'ok':
                rec['detail'] = v.detail
        elif s.route_pin_cheap == 'always' or (s.route_pin_cheap == 'if_unset' and not requested):
            target, source = s.route_cheap_model, 'pin_cheap'
        else:
            return None, {**rec, 'status': 'unchanged'}
    if not target or target == requested:
        return None, {**rec, 'status': rec.get('status', 'ok'), 'model_set': requested, 'source': source,
                      'changed': False}
    rec.update(status=rec.get('status', 'ok'), model_set=target, source=source, changed=True)
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'updatedInput': {**tin, 'model': target}}}, rec


def process_stop(payload, s, verifier, state):
    """B. -> (hook output dict or None, log record)."""
    event = payload.get('hook_event_name') or 'Stop'
    rec = {'hook': 'stop', 'hook_event': event, 'stop_hook_active': payload.get('stop_hook_active')}
    session = payload.get('session_id', '')
    tpath = payload.get('agent_transcript_path') if event == 'SubagentStop' else None
    user, last_from_t = transcript_messages(tpath or payload.get('transcript_path'))
    last = payload.get('last_assistant_message')
    last = last if isinstance(last, str) and last.strip() else last_from_t
    # Loop guard, two counters: per user request (task hash) and per chain of consecutive blocked stops. The chain
    # counter covers the case where a block reason is recorded as a new "user" message (the task hash changes): while
    # stop_hook_active is true the chain keeps counting; a stop with stop_hook_active false starts a new chain.
    key = f'{session}|{event}|{_h(user or "", 16)}'
    chain = f'{session}|{event}|chain'
    active = payload.get('stop_hook_active') is True
    if not active:
        state.reset_blocks(chain)
    n = max(state.blocks(key), state.blocks(chain) if active else 0)
    rec.update(task_sha=_h(user or '', 12), last_chars=len(last or ''), blocks_before=n,
               has_last_assistant_message=isinstance(payload.get('last_assistant_message'), str))

    def block():
        state.add_block(key)
        state.add_block(chain)
    if s.probe:
        rec.update(status='probe', payload_keys=sorted(payload))
        if n >= 1:
            return None, {**rec, 'decision': 'allow'}
        block()
        marker = f'DAVI-PROBE-B-{s.probe_nonce}'
        return {'decision': 'block', 'reason': f'Hook probe: reply with exactly {marker} and then stop.'}, \
            {**rec, 'decision': 'block', 'marker': marker}
    if not user or not last:
        return None, {**rec, 'status': 'no_input'}
    if n >= s.stop_max_blocks:
        return None, {**rec, 'status': 'max_blocks', 'decision': 'allow'}
    v = verifier.verify(user, last, kind='completion')
    rec.update(judge=v.judge, p_accept=v.p_accept, outcome=v.outcome, threshold=s.stop_threshold,
               latency_s=round(v.latency_s, 4), cost_usd=v.cost_usd, judge_model=v.model)
    if v.p_accept is None:
        decision = 'block' if s.on_error == 'escalate' else 'allow'
    else:
        decision = 'block' if v.p_accept < s.stop_threshold else 'allow'
    rec.update(status='ok' if v.outcome == 'ok' else 'verifier_failed', decision=decision)
    if v.outcome != 'ok':
        rec['detail'] = v.detail
    if decision != 'block':
        return None, rec
    block()
    p = v.p_accept if v.p_accept is None else round(v.p_accept, 3)
    reason = (f'[davi-verify] An automated completion check rated the task as possibly not complete '
              f'(P(complete)={p}). Check the requested final state against the user\'s request; finish any missing '
              'step. If it is already complete, state the evidence briefly and stop.')
    return {'decision': 'block', 'reason': reason}, rec


# --- entry point -----------------------------------------------------------------------------------------------------
def enabled(kind, env):
    """Cheap check used by the launcher before importing anything heavy."""
    try:
        if _flag(env, 'probe', False):
            return True
        if kind in ('pre', 'post'):
            return _flag(env, 'route_enabled', False)
        return _flag(env, 'stop_verify_enabled', False)
    except ConfigError:
        return True          # let main() report the config error


def _log(rec, s, stderr):
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
    try:
        print(line, file=stderr)
    except Exception:  # noqa: BLE001
        pass
    path = s.log_file if s is not None else None
    if path:
        try:
            with open(os.path.expanduser(path), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass


def main(kind, stdin=None, stdout=None, stderr=None, env=None, transport=None, verifier=None, now=time.time):
    """Run one hook. Always returns 0; prints a hook output JSON only when it changes something."""
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    env = dict(os.environ if env is None else env)
    t0 = time.perf_counter()
    rec = {'event': LOG_EVENT, 'kind': kind, 'ts': round(now(), 3)}
    s = None
    try:
        s = load_settings(env)
    except ConfigError as e:
        _log({**rec, 'status': 'config_error', 'detail': str(e)}, None, stderr)
        return 0
    try:
        payload = json.loads(stdin.read() or '{}')
    except ValueError:
        _log({**rec, 'status': 'bad_payload'}, s, stderr)
        return 0
    rec['session'] = _h(payload.get('session_id', ''), 12)
    if not enabled(kind, env):
        return 0
    if kind in ('pre', 'post') and payload.get('tool_name') not in AGENT_TOOLS:
        return 0
    if kind == 'subagent_stop' and not (s.stop_subagents or s.probe):
        return 0
    if kind in ('stop', 'subagent_stop') and s.probe and not s.probe_stop:
        return 0
    state = State(state_dir(env), now=now)

    def run():
        judge = s.route_judge if kind in ('pre', 'post') else (s.stop_judge or s.route_judge)
        needs = not s.probe and not (kind == 'pre' and s.route_mode == 'post') \
            and not (kind == 'post' and (s.route_mode == 'pre' or (s.route_mode == 'tiers' and not s.route_tier_check)))
        ver = verifier
        if ver is None and needs:
            ver = V.build_verifier(judge, env, transport=transport)
        try:
            if kind == 'pre':
                return process_pre(payload, s, state, ver if needs else None)
            if kind == 'post':
                return process_post(payload, s, ver, state)
            return process_stop(payload, s, ver, state)
        finally:
            if ver is not None and verifier is None:
                ver.close()

    from ..plugin_common import run_with_budget
    finished, value = run_with_budget(run, s.wall_budget_seconds)
    if not finished:
        _log({**rec, 'status': 'wall_budget', 'seconds': round(time.perf_counter() - t0, 3)}, s, stderr)
        return 0
    if isinstance(value, tuple) and len(value) == 2 and value[0] == 'error':
        _log({**rec, 'status': 'error', 'detail': value[1]}, s, stderr)
        return 0
    out, r = value
    if out is not None:
        try:
            stdout.write(json.dumps(out, ensure_ascii=False))
            stdout.flush()
        except Exception:  # noqa: BLE001
            pass
    _log({**rec, **r, 'emitted': out is not None, 'seconds': round(time.perf_counter() - t0, 3)}, s, stderr)
    return 0
