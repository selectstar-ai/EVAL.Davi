# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Claude Code hooks of the davi-route plugin.

Loaded by ``plugins/davi-route/hooks/route_hook.py``. Every hook is off by
default and fail-open: an error leaves Claude Code's behaviour unchanged.
Each call writes one JSON log line.

Hooks (one class each, sharing ``RouteHook``):

- ``AgentPreHook``   PreToolUse on the Agent tool. Sets the subagent's model
                     through ``updatedInput``: a pending escalation first;
                     else, by ``route_mode``, a 3-tier difficulty route
                     (``tiers``), an up-front route (``pre`` / ``both``) or
                     the cheap model (``post``, ``route_pin_cheap``).
- ``AgentPostHook``  PostToolUse on the Agent tool. Judges the subagent's
                     answer; below ``route_threshold`` it asks the main agent
                     to re-run the task with the strong model and stores
                     that model as the session's pending escalation.
- ``StopHook``       Stop (and SubagentStop when ``stop_subagents``). Judges
                     whether the user's request is complete and blocks the
                     stop at most ``stop_max_blocks`` times per request.

``route_mode``:

- ``tiers``  difficulty (kind ``tier``) picks the cheap, mid or strong model;
             with ``route_tier_check`` cheap answers are also judged.
- ``post``   cheap model first, the answer is judged (the default).
- ``pre``    the task alone picks the cheap or the strong model.
- ``both``   ``pre``, then answers of the cheap model are judged.

A hook cannot switch the main session's model; the model is set per Agent
call. Probe mode (``DAVI_ROUTE_PROBE=1``) calls no verifier and checks the
mechanisms themselves (``davi-route probe``).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .config import ConfigError, opt, opt_choice, opt_flag, opt_number
from .policy import RoutePolicy, TierPolicy, same_model
from .verifiers import JUDGES, build_verifier

AGENT_TOOLS = ('Agent', 'Task')
LOG_EVENT = 'davi-route'
STATE_DIRNAME = 'route-state'
PENDING_TTL_SECONDS = 1800
PIN_MODES = ('always', 'if_unset', 'off')
POST_MODES = ('context', 'block')
ROUTE_MODES = ('post', 'pre', 'both', 'tiers')
HOOK_KINDS = ('pre', 'post', 'stop', 'subagent_stop')


# --- settings ---------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Plugin options (``DAVI_ROUTE_<KEY>`` over ``CLAUDE_PLUGIN_OPTION_``)."""

    route_enabled: bool = False
    route_mode: str = 'post'
    route_judge: str = 'davi'
    route_threshold: float = 0.5
    route_pre_threshold: float = 0.5
    route_cheap_model: str = 'haiku'
    route_strong_model: str = 'opus'
    route_mid_model: str = 'sonnet'
    # KLEAR-fitted (haiku / sonnet / opus 5.5); recalibrate per workload
    route_tier_low: float = 0.48
    route_tier_high: float = 0.78
    route_tier_check: bool = False
    route_pin_cheap: str = 'always'
    route_post_mode: str = 'context'
    route_agent_types: tuple = ()  # empty = every subagent type
    stop_verify_enabled: bool = False
    stop_judge: str = ''  # empty = route_judge
    stop_threshold: float = 0.5
    stop_max_blocks: int = 1
    stop_subagents: bool = False
    on_error: str = 'accept'
    wall_budget_seconds: float = 25.0
    log_file: str = ''
    probe: bool = False
    probe_nonce: str = 'X'
    probe_pre_model: str = ''
    probe_stop: bool = True  # probe mode only: False skips the Stop probe

    def __post_init__(self):
        if self.route_tier_low > self.route_tier_high:
            raise ConfigError(
                'route_tier_low must not be above route_tier_high'
            )

    @classmethod
    def from_env(cls, env):
        """Read every option; raises ConfigError on an unusable value."""
        agent_types = tuple(
            name.strip()
            for name in (opt(env, 'route_agent_types') or '').split(',')
            if name.strip()
        )
        route_judge = opt_choice(env, 'route_judge', 'davi', JUDGES)
        return cls(
            route_enabled=opt_flag(env, 'route_enabled', False),
            route_mode=opt_choice(env, 'route_mode', 'post', ROUTE_MODES),
            route_judge=route_judge,
            route_threshold=_unit(env, 'route_threshold', 0.5),
            route_pre_threshold=_unit(env, 'route_pre_threshold', 0.5),
            route_cheap_model=opt(env, 'route_cheap_model', 'haiku'),
            route_strong_model=opt(env, 'route_strong_model', 'opus'),
            route_mid_model=opt(env, 'route_mid_model', 'sonnet'),
            route_tier_low=_unit(env, 'route_tier_low', 0.48),
            route_tier_high=_unit(env, 'route_tier_high', 0.78),
            route_tier_check=opt_flag(env, 'route_tier_check', False),
            route_pin_cheap=opt_choice(
                env, 'route_pin_cheap', 'always', PIN_MODES
            ),
            route_post_mode=opt_choice(
                env, 'route_post_mode', 'context', POST_MODES
            ),
            route_agent_types=agent_types,
            stop_verify_enabled=opt_flag(env, 'stop_verify_enabled', False),
            stop_judge=opt_choice(env, 'stop_judge', route_judge, JUDGES),
            stop_threshold=_unit(env, 'stop_threshold', 0.5),
            stop_max_blocks=opt_number(
                env, 'stop_max_blocks', 1, low=0, high=5, cast=int
            ),
            stop_subagents=opt_flag(env, 'stop_subagents', False),
            on_error=opt_choice(
                env, 'on_error', 'accept', ('accept', 'escalate')
            ),
            wall_budget_seconds=opt_number(
                env, 'wall_budget_seconds', 25.0, low=1.0, high=120.0
            ),
            log_file=opt(env, 'log_file', ''),
            probe=opt_flag(env, 'probe', False),
            probe_nonce=opt(env, 'probe_nonce', 'X'),
            probe_pre_model=opt(env, 'probe_pre_model', ''),
            probe_stop=opt_flag(env, 'probe_stop', True),
        )

    @property
    def answer_policy(self):
        """Judging a subagent's answer (kind ``answer``)."""
        return RoutePolicy(
            self.route_threshold,
            self.route_strong_model,
            self.route_cheap_model,
            self.on_error,
        )

    @property
    def pre_policy(self):
        """Routing a task before it runs (kind ``pre``)."""
        return RoutePolicy(
            self.route_pre_threshold,
            self.route_strong_model,
            self.route_cheap_model,
            self.on_error,
        )

    @property
    def tier_policy(self):
        """3-tier routing (kind ``tier``)."""
        return TierPolicy(
            self.route_tier_low,
            self.route_tier_high,
            self.route_cheap_model,
            self.route_mid_model,
            self.route_strong_model,
            self.on_error,
        )

    def judge_for(self, kind):
        """The judge name a hook kind uses."""
        if kind in ('pre', 'post'):
            return self.route_judge
        return self.stop_judge or self.route_judge

    def needs_verifier(self, kind):
        """Whether hook ``kind`` calls a verifier in this configuration."""
        if self.probe:
            return False
        if kind == 'pre':
            return self.route_mode != 'post'
        if kind == 'post':
            skip_tiers = self.route_mode == 'tiers' and not (
                self.route_tier_check
            )
            return not (self.route_mode == 'pre' or skip_tiers)
        return True


def _unit(env, key, default):
    return opt_number(env, key, default, low=0.0, high=1.0)


# --- local state ------------------------------------------------------------
def short_hash(text, length=24):
    """A short, stable digest (no text is ever logged)."""
    digest = hashlib.sha256((text or '').encode('utf-8')).hexdigest()
    return digest[:length]


def state_dir(env):
    """Where pending escalations and stop counters live."""
    override = env.get('DAVI_ROUTE_STATE_DIR')
    if isinstance(override, str) and override.strip():
        return override.strip()
    data = env.get('CLAUDE_PLUGIN_DATA')
    if isinstance(data, str) and data.strip():
        return os.path.join(data, STATE_DIRNAME)
    uid = os.getuid() if hasattr(os, 'getuid') else 'user'
    return os.path.join(
        tempfile.gettempdir(), f'davi-route-{STATE_DIRNAME}-{uid}'
    )


class State:
    """Tiny per-session JSON files, replaced atomically: the pending next
    model (one-shot, with a TTL) and the Stop block counters."""

    def __init__(self, root, now=time.time):
        self._root = root
        self._now = now

    def set_pending(self, session, model, reason):
        record = {'model': model, 'reason': reason, 'at': self._now()}
        self._write('pending', session, record)

    def take_pending(self, session):
        """The pending model record, removed on read; None if stale."""
        record = self._read('pending', session)
        if not record:
            return None
        self._remove('pending', session)
        age = self._now() - float(record.get('at', 0))
        if age > PENDING_TTL_SECONDS:
            return None
        if not isinstance(record.get('model'), str):
            return None
        return record

    def blocks(self, key):
        record = self._read('stop', key) or {}
        return int(record.get('blocks', 0))

    def add_block(self, key):
        record = {'blocks': self.blocks(key) + 1, 'at': self._now()}
        self._write('stop', key, record)

    def reset_blocks(self, key):
        self._remove('stop', key)

    def _path(self, kind, key):
        return os.path.join(self._root, kind, short_hash(key) + '.json')

    def _read(self, kind, key):
        try:
            with open(self._path(kind, key), encoding='utf-8') as handle:
                value = json.load(handle)
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _write(self, kind, key, value):
        path = self._path(kind, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f'{path}.{os.getpid()}.tmp'
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(value, handle)
        os.replace(temporary, path)

    def _remove(self, kind, key):
        try:
            os.remove(self._path(kind, key))
        except OSError:
            pass


# --- payload helpers --------------------------------------------------------
def response_text(response):
    """Text of a tool response of unknown shape (a string, a content-block
    list, nested dicts)."""
    if isinstance(response, str):
        return response
    texts = []

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            if node.get('type') == 'text' and isinstance(
                node.get('text'), str
            ):
                texts.append(node['text'])
                return
            for key in ('content', 'result', 'output', 'text', 'message'):
                if key in node:
                    walk(node[key], depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, str) and depth > 0:
            texts.append(node)

    walk(response)
    if texts:
        return '\n'.join(texts)
    return json.dumps(response, ensure_ascii=False, default=str)


def subagent_stats(response):
    """Numbers about a finished subagent from the Agent tool response
    (duration, tokens, tool uses); never its text."""
    if not isinstance(response, dict):
        return {}
    usage = response.get('usage')
    usage = usage if isinstance(usage, dict) else {}
    found = {
        'subagent_ms': response.get('totalDurationMs'),
        'subagent_tokens': response.get('totalTokens'),
        'subagent_tool_uses': response.get('totalToolUseCount'),
        'subagent_output_tokens': usage.get('output_tokens'),
    }
    stats = {
        key: value
        for key, value in found.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    status = response.get('status')
    if isinstance(status, str) and len(status) <= 32:
        stats['subagent_status'] = status
    stats['response_keys'] = sorted(str(key) for key in response)[:20]
    return stats


def payload_shape(node, depth=0):
    """Keys and types only, never values (for probe logs)."""
    if depth > 3:
        return type(node).__name__
    if isinstance(node, dict):
        items = list(node.items())[:30]
        return {key: payload_shape(value, depth + 1) for key, value in items}
    if isinstance(node, list):
        return [payload_shape(node[0], depth + 1)] if node else []
    return type(node).__name__


def _user_text(content):
    """User message text, or None for a tool-result message."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'tool_result':
            return None
    texts = [
        block.get('text', '')
        for block in content
        if isinstance(block, dict) and block.get('type') == 'text'
    ]
    return '\n'.join(texts) if texts else None


def transcript_messages(path, max_bytes=20_000_000):
    """(last user prompt, last assistant text) of a transcript JSONL.

    A missing file gives (None, None).
    """
    if not path or not os.path.isfile(path):
        return None, None
    user = assistant = None
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            size = os.path.getsize(path)
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()
            for line in handle:
                user, assistant = _scan_line(line, user, assistant)
    except OSError:
        return None, None
    return user, assistant


def _scan_line(line, user, assistant):
    try:
        entry = json.loads(line)
    except ValueError:
        return user, assistant
    message = entry.get('message') if isinstance(entry, dict) else None
    if not isinstance(message, dict):
        return user, assistant
    if entry.get('type') == 'user' and not entry.get('isMeta'):
        text = _user_text(message.get('content'))
        if text and text.strip() and not text.lstrip().startswith('<'):
            user = text
    elif entry.get('type') == 'assistant':
        text = response_text(message.get('content'))
        if text and text.strip():
            assistant = text
    return user, assistant


# --- hooks ------------------------------------------------------------------
class RouteHook(ABC):
    """One Claude Code hook: ``handle(payload) -> (output, log record)``.

    ``output`` is the hook output JSON, or None to change nothing.
    """

    def __init__(self, settings, state, verifier=None):
        self._settings = settings
        self._state = state
        self._verifier = verifier

    @abstractmethod
    def handle(self, payload):
        """Return (hook output or None, log record)."""

    @staticmethod
    def _verdict_fields(verdict):
        fields = {
            'judge': verdict.judge,
            'outcome': verdict.outcome,
            'latency_s': round(verdict.latency_s, 4),
            'cost_usd': verdict.cost_usd,
            'judge_model': verdict.model,
            'status': 'ok' if verdict.outcome == 'ok' else 'verifier_failed',
        }
        if verdict.outcome != 'ok':
            fields['detail'] = verdict.detail  # keys already scrubbed
        return fields


class AgentToolHook(RouteHook):
    """Shared reading of an Agent tool call."""

    @staticmethod
    def _tool_input(payload):
        tool_input = payload.get('tool_input')
        return tool_input if isinstance(tool_input, dict) else {}

    @staticmethod
    def _task_of(tool_input):
        return tool_input.get('prompt') or tool_input.get('description') or ''

    def _skips_agent_type(self, tool_input):
        wanted = self._settings.route_agent_types
        return bool(wanted) and tool_input.get('subagent_type') not in wanted


class AgentPreHook(AgentToolHook):
    """PreToolUse: choose the subagent's model."""

    def handle(self, payload):
        settings = self._settings
        tool_input = self._tool_input(payload)
        requested = tool_input.get('model')
        record = {
            'hook': 'pre',
            'tool': payload.get('tool_name'),
            'agent_type': tool_input.get('subagent_type'),
            'model_requested': requested,
        }
        if settings.probe:
            record.update(
                status='probe',
                tool_input_shape=payload_shape(tool_input),
                payload_keys=sorted(payload),
            )
            if not settings.probe_pre_model:
                return None, record
            return self._apply(
                tool_input, settings.probe_pre_model, 'probe', record
            )
        if self._skips_agent_type(tool_input):
            return None, {**record, 'status': 'type_skipped'}
        pending = self._state.take_pending(payload.get('session_id', ''))
        task = self._task_of(tool_input)
        can_judge = self._verifier is not None and bool(task.strip())
        if pending:
            return self._apply(
                tool_input, pending['model'], 'escalation', record
            )
        if settings.route_mode == 'tiers' and can_judge:
            target = self._route_by_tier(task, record)
            return self._apply(tool_input, target, 'tier_route', record)
        if settings.route_mode in ('pre', 'both') and can_judge:
            target = self._route_up_front(task, record)
            return self._apply(tool_input, target, 'pre_route', record)
        pin = settings.route_pin_cheap
        if pin == 'always' or (pin == 'if_unset' and not requested):
            return self._apply(
                tool_input, settings.route_cheap_model, 'pin_cheap', record
            )
        return None, {**record, 'status': 'unchanged'}

    def _route_by_tier(self, task, record):
        policy = self._settings.tier_policy
        verdict = self._verifier.verify(task, kind='tier')
        tier = policy.tier_of(verdict.level)
        record.update(
            self._verdict_fields(verdict),
            level=verdict.level,
            level_probabilities=verdict.levels,
            tier=tier,
            task_sha=short_hash(task, 12),
            tier_low=policy.low,
            tier_high=policy.high,
        )
        return policy.model_for(tier)

    def _route_up_front(self, task, record):
        settings = self._settings
        verdict = self._verifier.verify(task, kind='pre')
        route = settings.pre_policy.decide(verdict.p_accept)
        record.update(
            self._verdict_fields(verdict),
            p_accept=verdict.p_accept,
            decision=route.decision,
            reason=route.reason,
            threshold=settings.route_pre_threshold,
            task_sha=short_hash(task, 12),
        )
        if route.decision == 'escalate':
            return route.next_model
        return settings.route_cheap_model

    @staticmethod
    def _apply(tool_input, target, source, record):
        requested = tool_input.get('model')
        status = record.get('status', 'ok')
        if not target or target == requested:
            return None, {
                **record,
                'status': status,
                'model_set': requested,
                'source': source,
                'changed': False,
            }
        record.update(
            status=status, model_set=target, source=source, changed=True
        )
        output = {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'updatedInput': {**tool_input, 'model': target},
            }
        }
        return output, record


class AgentPostHook(AgentToolHook):
    """PostToolUse: judge the subagent's answer, escalate when needed."""

    def handle(self, payload):
        settings = self._settings
        tool_input = self._tool_input(payload)
        response = payload.get('tool_response')
        model_in = tool_input.get('model')
        resolved = (
            response.get('resolvedModel')
            if isinstance(response, dict)
            else None
        )
        record = {
            'hook': 'post',
            'tool': payload.get('tool_name'),
            'agent_type': tool_input.get('subagent_type'),
            'model_in': model_in,
            'resolved_model': resolved,
            **subagent_stats(response),
        }
        if settings.probe:
            return self._probe(payload, tool_input, record)
        skipped = self._skip_reason(tool_input, [model_in, resolved])
        if skipped:
            return None, {**record, **skipped}
        task = self._task_of(tool_input)
        result = response_text(response)
        record.update(task_sha=short_hash(task, 12), result_chars=len(result))
        if not task.strip() or not result.strip():
            return None, {**record, 'status': 'no_input'}
        verdict = self._verifier.verify(task, result, kind='answer')
        route = settings.answer_policy.decide(
            verdict.p_accept, current_model=model_in
        )
        record.update(
            self._verdict_fields(verdict),
            p_accept=verdict.p_accept,
            decision=route.decision,
            reason=route.reason,
            next_model=route.next_model,
            threshold=settings.route_threshold,
        )
        if route.decision != 'escalate':
            return None, record
        self._state.set_pending(
            payload.get('session_id', ''), route.next_model, route.reason
        )
        return self._escalation(verdict.p_accept, route.next_model), record

    def _skip_reason(self, tool_input, models):
        """Log fields when the answer is not judged, else None."""
        settings = self._settings
        ran = [model for model in models if model]
        if settings.route_mode == 'pre':
            return {'status': 'mode_pre'}
        if settings.route_mode == 'tiers':
            if not settings.route_tier_check:
                return {'status': 'mode_tiers'}
            cheap = settings.route_cheap_model
            if ran and not any(same_model(model, cheap) for model in ran):
                # mid and strong answers are not re-checked
                return {'status': 'mode_tiers', 'reason': 'not_cheap'}
        if self._skips_agent_type(tool_input):
            return {'status': 'type_skipped'}
        strong = settings.route_strong_model
        if any(same_model(model, strong) for model in ran):
            return {
                'status': 'ok',
                'decision': 'accept',
                'reason': 'already_strong',
            }
        return None

    def _escalation(self, p_accept, next_model):
        settings = self._settings
        shown = p_accept if p_accept is None else round(p_accept, 3)
        message = (
            '[davi-route] An automated verifier rated this subagent result '
            f'as likely not acceptable (P(accept)={shown} < '
            f'{settings.route_threshold}). Do not use it as the final '
            'result. Re-run the same task with the Agent tool, passing '
            f'model "{next_model}" and the same prompt, and use that result '
            'instead.'
        )
        if settings.route_post_mode == 'block':
            return {'decision': 'block', 'reason': message}
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'additionalContext': message,
            }
        }

    def _probe(self, payload, tool_input, record):
        marker = f'DAVI-PROBE-A-{self._settings.probe_nonce}'
        record.update(
            status='probe',
            marker=marker,
            tool_input_shape=payload_shape(tool_input),
            tool_response_shape=payload_shape(payload.get('tool_response')),
            payload_keys=sorted(payload),
        )
        context = (
            f'Hook probe marker: {marker}. If you can read this line, '
            'include the marker verbatim in your final answer.'
        )
        output = {
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'additionalContext': context,
            }
        }
        return output, record


class StopHook(RouteHook):
    """Stop / SubagentStop: block an apparently unfinished stop, once.

    Two loop guards count blocks: one per user request (a hash of the
    request) and one per chain of consecutive blocked stops, which covers
    a block reason recorded as a new "user" message. While
    ``stop_hook_active`` is true the chain keeps counting; a stop with it
    false starts a new chain.
    """

    def handle(self, payload):
        settings = self._settings
        event = payload.get('hook_event_name') or 'Stop'
        record = {
            'hook': 'stop',
            'hook_event': event,
            'stop_hook_active': payload.get('stop_hook_active'),
        }
        session = payload.get('session_id', '')
        user, last = self._messages(payload, event)
        request_key = f'{session}|{event}|{short_hash(user or "", 16)}'
        chain_key = f'{session}|{event}|chain'
        active = payload.get('stop_hook_active') is True
        if not active:
            self._state.reset_blocks(chain_key)
        chain_blocks = self._state.blocks(chain_key) if active else 0
        blocks = max(self._state.blocks(request_key), chain_blocks)
        record.update(
            task_sha=short_hash(user or '', 12),
            last_chars=len(last or ''),
            blocks_before=blocks,
            has_last_assistant_message=isinstance(
                payload.get('last_assistant_message'), str
            ),
        )
        keys = (request_key, chain_key)
        if settings.probe:
            return self._probe(payload, record, blocks, keys)
        if not user or not last:
            return None, {**record, 'status': 'no_input'}
        if blocks >= settings.stop_max_blocks:
            return None, {
                **record,
                'status': 'max_blocks',
                'decision': 'allow',
            }
        verdict = self._verifier.verify(user, last, kind='completion')
        decision = self._decision(verdict.p_accept)
        record.update(
            self._verdict_fields(verdict),
            p_accept=verdict.p_accept,
            threshold=settings.stop_threshold,
            decision=decision,
        )
        if decision != 'block':
            return None, record
        self._block(keys)
        return {'decision': 'block', 'reason': self._reason(verdict)}, record

    @staticmethod
    def _messages(payload, event):
        transcript = None
        if event == 'SubagentStop':
            transcript = payload.get('agent_transcript_path')
        user, last_in_transcript = transcript_messages(
            transcript or payload.get('transcript_path')
        )
        last = payload.get('last_assistant_message')
        if not (isinstance(last, str) and last.strip()):
            last = last_in_transcript
        return user, last

    def _decision(self, p_accept):
        settings = self._settings
        if p_accept is None:
            return 'block' if settings.on_error == 'escalate' else 'allow'
        return 'block' if p_accept < settings.stop_threshold else 'allow'

    def _block(self, keys):
        for key in keys:
            self._state.add_block(key)

    @staticmethod
    def _reason(verdict):
        shown = verdict.p_accept
        if shown is not None:
            shown = round(shown, 3)
        return (
            '[davi-verify] An automated completion check rated the task as '
            f'possibly not complete (P(complete)={shown}). Check the '
            "requested final state against the user's request; finish any "
            'missing step. If it is already complete, state the evidence '
            'briefly and stop.'
        )

    def _probe(self, payload, record, blocks, keys):
        record.update(status='probe', payload_keys=sorted(payload))
        if blocks >= 1:
            return None, {**record, 'decision': 'allow'}
        self._block(keys)
        marker = f'DAVI-PROBE-B-{self._settings.probe_nonce}'
        output = {
            'decision': 'block',
            'reason': f'Hook probe: reply with exactly {marker} and then '
            'stop.',
        }
        return output, {**record, 'decision': 'block', 'marker': marker}


HOOK_CLASSES = {
    'pre': AgentPreHook,
    'post': AgentPostHook,
    'stop': StopHook,
    'subagent_stop': StopHook,
}


# --- entry point ------------------------------------------------------------
def enabled(kind, env):
    """Cheap check used by the launcher before importing anything heavy."""
    try:
        if opt_flag(env, 'probe', False):
            return True
        if kind in ('pre', 'post'):
            return opt_flag(env, 'route_enabled', False)
        return opt_flag(env, 'stop_verify_enabled', False)
    except ConfigError:
        return True  # let main() report the config error


def write_log(record, settings, stream):
    """One JSON line to ``stream`` and, if configured, to the log file."""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    try:
        print(line, file=stream)
    except Exception:  # noqa: BLE001 - logging never breaks a hook
        pass
    path = settings.log_file if settings is not None else None
    if not path:
        return
    try:
        with open(os.path.expanduser(path), 'a', encoding='utf-8') as handle:
            handle.write(line + '\n')
    except OSError:
        pass


def _applies(kind, payload, settings):
    if kind in ('pre', 'post'):
        return payload.get('tool_name') in AGENT_TOOLS
    if kind == 'subagent_stop' and not (
        settings.stop_subagents or settings.probe
    ):
        return False
    if kind in ('stop', 'subagent_stop'):
        return not (settings.probe and not settings.probe_stop)
    return True


def main(
    kind,
    stdin=None,
    stdout=None,
    stderr=None,
    env=None,
    transport=None,
    verifier=None,
    now=time.time,
):
    """Run one hook. Always returns 0; prints hook output JSON only when it
    changes something."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = dict(os.environ if env is None else env)
    started = time.perf_counter()
    record = {'event': LOG_EVENT, 'kind': kind, 'ts': round(now(), 3)}
    try:
        settings = Settings.from_env(env)
    except ConfigError as error:
        write_log(
            {**record, 'status': 'config_error', 'detail': str(error)},
            None,
            stderr,
        )
        return 0
    try:
        payload = json.loads(stdin.read() or '{}')
    except ValueError:
        write_log({**record, 'status': 'bad_payload'}, settings, stderr)
        return 0
    record['session'] = short_hash(payload.get('session_id', ''), 12)
    if not enabled(kind, env) or not _applies(kind, payload, settings):
        return 0
    state = State(state_dir(env), now=now)

    def run():
        judge = verifier
        owned = False
        if judge is None and settings.needs_verifier(kind):
            judge = build_verifier(
                settings.judge_for(kind), env, transport=transport
            )
            owned = True
        if not settings.needs_verifier(kind) and kind == 'pre':
            judge = None
        try:
            hook = HOOK_CLASSES[kind](settings, state, judge)
            return hook.handle(payload)
        finally:
            if owned:
                judge.close()

    from ..plugin_common import run_with_budget

    finished, value = run_with_budget(run, settings.wall_budget_seconds)
    seconds = round(time.perf_counter() - started, 3)
    if not finished:
        write_log(
            {**record, 'status': 'wall_budget', 'seconds': seconds},
            settings,
            stderr,
        )
        return 0
    if isinstance(value, tuple) and len(value) == 2 and value[0] == 'error':
        write_log(
            {**record, 'status': 'error', 'detail': value[1]},
            settings,
            stderr,
        )
        return 0
    output, hook_record = value
    if output is not None:
        try:
            stdout.write(json.dumps(output, ensure_ascii=False))
            stdout.flush()
        except Exception:  # noqa: BLE001 - output errors never break Claude
            pass
    write_log(
        {
            **record,
            **hook_record,
            'emitted': output is not None,
            'seconds': seconds,
        },
        settings,
        stderr,
    )
    return 0
