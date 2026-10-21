"""Routing: verifiers, policies, Claude Code hooks, grading, harness,
analysis and probes.

Model-free: every HTTP call goes to an in-process MockTransport; claude -p
is replaced by a fake runner.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

import httpx
import pytest

from judge_adapter.keys import CredentialMissing
from judge_adapter.routing import (
    analysis,
    bundle,
    cli,
    grading,
    harness,
    probe,
    verifiers,
)
from judge_adapter.routing import hooks as route_hooks
from judge_adapter.routing.config import ConfigError, opt_number, unquote
from judge_adapter.routing.hooks import (
    AgentPostHook,
    AgentPreHook,
    Settings,
    State,
    StopHook,
)
from judge_adapter.routing.policy import RoutePolicy, TierPolicy, same_model
from judge_adapter.routing.questions import (
    KINDS,
    LABELS,
    PRE_ENSEMBLE,
    QUESTION_ID,
    clip,
)
from judge_adapter.routing.verifiers import FakeVerifier, Verdict

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins' / 'davi-route'
LAUNCHER = str(PLUGIN / 'hooks' / 'route_hook.py')
LAUNCHER_ARG = '${CLAUDE_PLUGIN_ROOT}/hooks/route_hook.py'
KEY = 'sk-fake-ROUTE-KEY-not-a-secret-0123456789'


def quiet(*_args):
    """A ``log`` that prints nothing."""


# --- policy -----------------------------------------------------------------
def test_policy_escalates_below_threshold_with_next_model_string():
    policy = RoutePolicy(threshold=0.6, strong_model='opus')
    route = policy.decide(0.59)
    assert route.decision == 'escalate' and route.next_model == 'opus'
    route = policy.decide(0.6)
    assert route.decision == 'accept' and route.next_model is None


def test_policy_never_escalates_the_strong_model_and_fails_open():
    policy = RoutePolicy(strong_model='opus')
    route = policy.decide(0.01, current_model='claude-opus-5-5')
    assert route.reason == 'already_strong'
    assert policy.decide(None).decision == 'accept'
    escalating = RoutePolicy(on_error='escalate')
    assert escalating.decide(None).decision == 'escalate'
    assert same_model('Opus', 'opus') and not same_model('haiku', 'opus')
    with pytest.raises(ValueError):
        RoutePolicy(threshold=1.5)


def test_tier_policy_cutoffs_and_failures():
    policy = TierPolicy(low=0.4, high=0.8)
    assert [policy.tier_of(x) for x in (0.1, 0.4, 0.79, 0.8)] == [
        'cheap',
        'mid',
        'mid',
        'strong',
    ]
    assert policy.tier_of(None) == 'cheap'
    assert TierPolicy(on_error='escalate').tier_of(None) == 'strong'
    assert policy.model_for('mid') == 'sonnet'
    with pytest.raises(ValueError):
        TierPolicy(low=0.9, high=0.2)


# --- config -----------------------------------------------------------------
def test_options_override_plugin_options_and_validate():
    env = {
        'DAVI_ROUTE_ROUTE_THRESHOLD': '0.7',
        'CLAUDE_PLUGIN_OPTION_ROUTE_THRESHOLD': '0.2',
        'CLAUDE_PLUGIN_OPTION_ROUTE_JUDGE': 'jev',
    }
    settings = Settings.from_env(env)
    assert settings.route_threshold == 0.7
    assert settings.route_judge == 'jev' and settings.stop_judge == 'jev'
    with pytest.raises(ConfigError):
        Settings.from_env({'DAVI_ROUTE_ROUTE_JUDGE': 'gpt'})
    assert unquote('"http://davi.test"') == 'http://davi.test'
    with pytest.raises(ConfigError):
        opt_number({'DAVI_ROUTE_X': '7'}, 'x', high=1.0)


# --- verifiers --------------------------------------------------------------
def jev_transport(seen, p=0.27, status=200, body=None):
    def handle(request):
        seen.append(request)
        if status != 200:
            auth = request.headers.get('authorization')
            return httpx.Response(status, text=f'nope {auth}')
        question_id = next(iter(json.loads(request.content)['questions']))
        answer = {
            'type': 'choice',
            'choice': 'accept' if p >= 0.5 else 'escalate',
            'probabilities': {'accept': p, 'escalate': 1 - p},
        }
        return httpx.Response(
            200,
            json=body
            or {
                'model': 'jev-1.13.0',
                'usage': {'input_tokens': 1000, 'output_tokens': 3},
                'answers': {question_id: answer},
            },
        )

    return httpx.MockTransport(handle)


def test_jev_wire_format_and_parse():
    seen = []
    verifier = verifiers.JevVerifier(
        lambda: KEY, transport=jev_transport(seen)
    )
    verdict = verifier.verify(
        'What is 2+2?', 'The answer is 5.', rubric='exact', kind='answer'
    )
    (request,) = seen
    assert request.url.path == '/v1/systemone'
    assert request.headers['authorization'] == f'Bearer {KEY}'
    body = json.loads(request.content)
    assert body['model'] == 'jev-latest'
    assert set(body['state']) == {'task', 'candidate', 'rubric'}
    question = body['questions'][QUESTION_ID]
    assert question['type'] == 'choice'
    assert set(question['criteria']) == {'accept', 'escalate'}
    assert verdict.outcome == 'ok' and verdict.model == 'jev-1.13.0'
    assert verdict.p_accept == pytest.approx(0.27)
    assert verdict.cost_usd == pytest.approx(1000 * 0.042 / 1e6)


def test_jev_failures_are_verdicts_and_never_leak_the_key():
    failing = verifiers.JevVerifier(
        lambda: KEY, transport=jev_transport([], status=500)
    )
    verdict = failing.verify('t', 'c')
    assert verdict.p_accept is None and verdict.outcome == 'http'
    assert KEY not in (verdict.detail or '')
    empty = jev_transport([], body={'answers': {}, 'usage': {}})
    verdict = verifiers.JevVerifier(lambda: KEY, transport=empty).verify(
        't', 'c'
    )
    assert verdict.outcome == 'format_error'

    def missing():
        raise CredentialMissing('no key')

    verdict = verifiers.JevVerifier(
        missing, transport=jev_transport([])
    ).verify('t', 'c')
    assert verdict.outcome == 'local_config' and verdict.cost_usd == 0.0


def test_verifiers_share_one_abstract_interface():
    with pytest.raises(TypeError):
        verifiers.Verifier()
    with pytest.raises(TypeError):
        verifiers.HttpVerifier(lambda: KEY, 'http://x')
    for cls in (
        FakeVerifier,
        verifiers.DaviVerifier,
        verifiers.JevVerifier,
        verifiers.ClaudeVerifier,
        verifiers.DaviRunPodVerifier,
    ):
        assert issubclass(cls, verifiers.Verifier)
    assert issubclass(verifiers.DaviVerifier, verifiers.SystemOneVerifier)
    fake = FakeVerifier()
    fake.verify('t', 'c')
    with pytest.raises(AttributeError):
        fake.calls = []
    with pytest.raises(ValueError):
        fake.verify('t', 'c', kind='unknown')


def claude_transport(seen, reply='23'):
    def handle(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                'model': 'claude-haiku-4-5-20251001',
                'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': reply}],
                'usage': {'input_tokens': 500, 'output_tokens': 2},
            },
        )

    return httpx.MockTransport(handle)


def test_claude_verbalized_probability():
    seen = []
    verifier = verifiers.ClaudeVerifier(
        lambda: KEY,
        model='claude-haiku-4-5-20251001',
        transport=claude_transport(seen, '23'),
    )
    verdict = verifier.verify('t', 'c', kind='completion')
    body = json.loads(seen[0].content)
    assert seen[0].headers['x-api-key'] == KEY
    assert body['max_tokens'] == 8 and body['temperature'] == 0.0
    instructions = KINDS['completion']['instructions']
    assert instructions in body['messages'][0]['content']
    assert verdict.p_accept == pytest.approx(0.23)
    assert verdict.cost_usd == pytest.approx(500 / 1e6 + 2 * 5 / 1e6)
    unsure = verifiers.ClaudeVerifier(
        lambda: KEY, transport=claude_transport([], 'maybe')
    )
    bad = unsure.verify('t', 'c')
    assert bad.outcome == 'format_error' and bad.p_accept is None
    assert verifiers.parse_percent('100') == 1.0
    assert verifiers.parse_percent('250') is None


def test_claude_5_judges_use_effort_and_read_the_last_number():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                'content': [
                    {'type': 'thinking', 'thinking': ''},
                    {'type': 'text', 'text': '17*23 = 391, correct.\n\n96'},
                ],
                'usage': {'input_tokens': 1000, 'output_tokens': 100},
            },
        )

    env = {
        'ANTHROPIC_API_KEY': 'sk-test-0000000000',
        'DAVI_ROUTE_CLAUDE_JUDGE_MODEL': 'claude-haiku-5-5',
    }
    verifier = verifiers.build_verifier(
        'claude', env, transport=httpx.MockTransport(handler)
    )
    verdict = verifier.verify('What is 17*23?', '391')
    assert verdict.p_accept == pytest.approx(0.96)
    expected = 1000 * 0.10 / 1e6 + 100 * 0.50 / 1e6
    assert verdict.cost_usd == pytest.approx(expected)
    assert 'temperature' not in seen[0]
    assert seen[0]['output_config'] == {'effort': 'low'}
    assert seen[0]['max_tokens'] >= 1024
    assert verifiers.uses_effort('claude-sonnet-5-5')
    assert not verifiers.uses_effort('claude-haiku-4-5-20251001')


def systemone_handler(seen, accept=0.81):
    def handle(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                'answers': {
                    'route': {
                        'probabilities': {
                            'accept': accept,
                            'escalate': 1 - accept,
                        }
                    }
                },
                'usage': {'input_tokens': 500, 'output_tokens': 1},
                'model': 'davi-x',
            },
        )

    return httpx.MockTransport(handle)


def test_davi_systemone_needs_no_key_and_has_unknown_cost():
    seen = []
    transport = systemone_handler(seen)
    base = {'DAVI_BASE_URL': 'http://davi.test:20100'}
    verdict = verifiers.build_verifier(
        'davi', base, transport=transport
    ).verify('What is 17*23?', '391')
    assert (verdict.judge, verdict.outcome, verdict.model) == (
        'davi',
        'ok',
        'davi-x',
    )
    assert verdict.p_accept == pytest.approx(0.81)
    assert verdict.cost_usd is None
    assert str(seen[0].url) == 'http://davi.test:20100/v1/systemone'
    assert 'authorization' not in seen[0].headers
    question = json.loads(seen[0].content)['questions']['route']
    assert question['type'] == 'choice'
    assert set(question['criteria']) == set(LABELS)
    priced = {**base, 'DAVI_ROUTE_DAVI_USD_IN_PER_MTOK': '0.02'}
    verdict = verifiers.build_verifier(
        'davi', priced, transport=transport
    ).verify('q', 'a')
    assert verdict.cost_usd == pytest.approx(500 * 0.02 / 1e6)
    quoted = {'DAVI_BASE_URL': '"http://davi.test:20100"'}
    verdict = verifiers.build_verifier(
        'davi', quoted, transport=transport
    ).verify('q', 'a')
    assert verdict.outcome == 'ok'
    assert str(seen[-1].url) == 'http://davi.test:20100/v1/systemone'


def test_davi_server_from_the_plugin_options():
    # the Claude app passes userConfig as CLAUDE_PLUGIN_OPTION_<KEY>
    seen = []
    transport = systemone_handler(seen)
    env = {
        'CLAUDE_PLUGIN_OPTION_DAVI_BASE_URL': 'http://option.test:1',
        'CLAUDE_PLUGIN_OPTION_DAVI_API_KEY': 'davi-key-0000000000',
        'DAVI_BASE_URL': 'http://shell.test:2',
    }
    verdict = verifiers.build_verifier(
        'davi', env, transport=transport
    ).verify('q', 'a')
    assert verdict.outcome == 'ok'
    assert str(seen[0].url) == 'http://option.test:1/v1/systemone'
    assert seen[0].headers['authorization'] == 'Bearer davi-key-0000000000'
    # an empty option (the manifest default) falls back to DAVI_BASE_URL
    env['CLAUDE_PLUGIN_OPTION_DAVI_BASE_URL'] = ''
    verifiers.build_verifier('davi', env, transport=transport).verify('q', 'a')
    assert str(seen[-1].url) == 'http://shell.test:2/v1/systemone'


def test_davi_pre_ensemble_asks_every_phrasing_and_averages_logits():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        answers = {}
        for question_id, question in body['questions'].items():
            yes = next(
                value[2]
                for name, value in PRE_ENSEMBLE.items()
                if question_id == f'{QUESTION_ID}_{name}'
            )
            other = next(o for o in question['criteria'] if o != yes)
            answers[question_id] = {
                'type': 'choice',
                'probabilities': {yes: 0.8, other: 0.2},
            }
        return httpx.Response(
            200,
            json={
                'answers': answers,
                'usage': {'input_tokens': 900, 'output_tokens': 0},
            },
        )

    env = {'DAVI_BASE_URL': 'http://davi.test'}
    verifier = verifiers.build_verifier(
        'davi', env, transport=httpx.MockTransport(handler)
    )
    verdict = verifier.verify('Who wrote Hamlet?', kind='pre')
    assert verdict.outcome == 'ok'
    assert verdict.p_accept == pytest.approx(0.8)
    assert len(seen) == 1
    assert len(seen[0]['questions']) == len(PRE_ENSEMBLE)
    assert 'candidate' not in seen[0]['state']
    single = {**env, 'DAVI_ROUTE_DAVI_PRE_ENSEMBLE': 'false'}
    verifier = verifiers.build_verifier(
        'davi', single, transport=systemone_handler([], accept=0.3)
    )
    verdict = verifier.verify('Who wrote Hamlet?', kind='pre')
    assert verdict.p_accept == pytest.approx(0.3)


def runpod_transport(bodies):
    def handle(request):
        if '/runsync' not in request.url.path:
            return httpx.Response(404)
        body = json.loads(request.content)
        bodies.append(body)
        question_id = next(iter(body['input']['questions']))
        mark = re.search(
            r'\[\[ACCEPT=([0-9.]+)\]\]',
            body['input']['state']['candidate'],
        )
        p = float(mark.group(1)) if mark else 0.5
        answer = {
            'type': 'choice',
            'choice': 'opt_accept' if p > 0.5 else 'opt_escalate',
            'probabilities': {'opt_accept': p, 'opt_escalate': 1 - p},
        }
        return httpx.Response(
            200,
            json={
                'id': 'j1',
                'status': 'COMPLETED',
                'delayTime': 10,
                'executionTime': 400,
                'output': {
                    'model': 'davi-0.2.0',
                    'answers': {question_id: answer},
                },
            },
        )

    return httpx.MockTransport(handle)


RUNPOD_ENV = {
    'DAVI_ROUTE_RUNPOD_API_KEY': KEY,
    'DAVI_ROUTE_CHOICE_ENDPOINT_ID': 'ep1',
    'DAVI_ROUTE_USD_PER_SECOND': '0.001',
}


def test_davi_verifier_through_the_existing_choice_backend():
    bodies = []
    verifier = verifiers.build_verifier(
        'davi', RUNPOD_ENV, transport=runpod_transport(bodies)
    )
    assert isinstance(verifier, verifiers.DaviRunPodVerifier)
    verdict = verifier.verify('task', 'answer [[ACCEPT=0.8]]')
    assert verdict.outcome == 'ok' and verdict.model == 'davi-0.2.0'
    assert verdict.p_accept == pytest.approx(0.8)
    assert verdict.cost_usd == pytest.approx(0.4 * 0.001)
    question = bodies[0]['input']['questions'][QUESTION_ID]
    assert set(question['criteria']) == {'opt_accept', 'opt_escalate'}
    # an exact tie has no decision, but P is still reported
    tie = verifier.verify('task', 'answer [[ACCEPT=0.5]]')
    assert tie.p_accept == pytest.approx(0.5)


def test_clip_keeps_head_and_tail():
    text = 'H' * 100 + 'M' * 1000 + 'T' * 100
    clipped = clip(text, 300)
    assert clipped.startswith('H') and clipped.endswith('T')
    assert 'omitted' in clipped and len(clipped) < 400


def test_tier_kind_on_systemone_claude_and_runpod():
    def davi(request):
        question = json.loads(request.content)['questions']['route']
        assert question['type'] == 'choice'
        assert list(question['criteria']) == ['1', '2', '3', '4', '5']
        return httpx.Response(
            200,
            json={
                'answers': {
                    'route': {
                        'probabilities': {
                            '1': 0.0,
                            '2': 0.0,
                            '3': 0.5,
                            '4': 0.5,
                            '5': 0.0,
                        },
                    }
                }
            },
        )

    env = {'DAVI_BASE_URL': 'http://davi.test'}
    verifier = verifiers.build_verifier(
        'davi', env, transport=httpx.MockTransport(davi)
    )
    verdict = verifier.verify('Who wrote Hamlet?', kind='tier')
    assert verdict.outcome == 'ok'
    assert verdict.level == pytest.approx(0.625)
    assert verdict.p_accept == pytest.approx(0.375)
    seen = []
    claude = verifiers.ClaudeVerifier(
        lambda: KEY,
        model='claude-haiku-5-5',
        transport=claude_transport(seen, 'Probably hard for it.\n\n4'),
    )
    assert claude.verify('q', kind='tier').level == pytest.approx(0.75)
    prompt = json.loads(seen[0].content)['messages'][0]['content']
    assert '1 to 5' in prompt
    runpod = verifiers.build_verifier(
        'davi', RUNPOD_ENV, transport=runpod_transport([])
    )
    assert runpod.verify('q', kind='tier').outcome == 'error'


# --- hooks ------------------------------------------------------------------
def agent_payload(
    event,
    model='haiku',
    result='[[ACCEPT=0.2]] wrong',
    session='s1',
    **extra,
):
    tool_input = {
        'subagent_type': 'general-purpose',
        'description': 'd',
        'prompt': 'compute 17*23',
    }
    if model:
        tool_input['model'] = model
    payload = {
        'session_id': session,
        'hook_event_name': event,
        'tool_name': 'Agent',
        'transcript_path': '/nonexistent',
        'tool_input': tool_input,
    }
    if event == 'PostToolUse':
        payload['tool_response'] = {
            'content': [{'type': 'text', 'text': result}],
            'totalTokens': 10,
        }
    payload.update(extra)
    return payload


def settings(**options):
    env = {
        'DAVI_ROUTE_ROUTE_ENABLED': 'true',
        'DAVI_ROUTE_STOP_VERIFY_ENABLED': 'true',
    }
    env.update(
        {
            f'DAVI_ROUTE_{key.upper()}': str(value)
            for key, value in options.items()
        }
    )
    return Settings.from_env(env)


def run_pre(payload, config, state, verifier=None):
    return AgentPreHook(config, state, verifier).handle(payload)


def run_post(payload, config, verifier, state):
    return AgentPostHook(config, state, verifier).handle(payload)


def run_stop(payload, config, verifier, state):
    return StopHook(config, state, verifier).handle(payload)


def updated_model(output, default=None):
    hook_output = (output or {}).get('hookSpecificOutput', {})
    return hook_output.get('updatedInput', {}).get('model', default)


def test_hooks_share_one_abstract_interface():
    with pytest.raises(TypeError):
        route_hooks.RouteHook(settings(), None)
    for cls in route_hooks.HOOK_CLASSES.values():
        assert issubclass(cls, route_hooks.RouteHook)


def test_post_escalates_and_pre_applies_the_strong_model_once(tmp_path):
    state, config = State(str(tmp_path)), settings()
    verifier = FakeVerifier()
    output, record = run_post(
        agent_payload('PostToolUse'), config, verifier, state
    )
    hook_output = output['hookSpecificOutput']
    assert hook_output['hookEventName'] == 'PostToolUse'
    assert 'model "opus"' in hook_output['additionalContext']
    assert record['decision'] == 'escalate'
    assert record['next_model'] == 'opus' and record['p_accept'] == 0.2
    assert verifier.calls[0]['task'] == 'compute 17*23'
    assert verifier.calls[0]['candidate'].startswith('[[ACCEPT=0.2]]')
    # the re-run
    output, record = run_pre(
        agent_payload('PreToolUse', model='haiku'), config, state
    )
    assert updated_model(output) == 'opus'
    assert record['source'] == 'escalation'
    assert output['hookSpecificOutput']['updatedInput']['prompt'] == (
        'compute 17*23'
    )
    # the pending escalation was one-shot
    output, record = run_pre(
        agent_payload('PreToolUse', model='sonnet'), config, state
    )
    assert updated_model(output) == 'haiku'
    assert record['source'] == 'pin_cheap'


def test_post_accepts_and_never_escalates_the_strong_model(tmp_path):
    state, config = State(str(tmp_path)), settings()
    good = agent_payload('PostToolUse', result='[[ACCEPT=0.9]] ok')
    assert run_post(good, config, FakeVerifier(), state)[0] is None
    output, record = run_post(
        agent_payload('PostToolUse', model='opus'),
        config,
        FakeVerifier(),
        state,
    )
    assert output is None and record['reason'] == 'already_strong'
    output, _ = run_post(
        agent_payload('PostToolUse'),
        settings(route_post_mode='block'),
        FakeVerifier(),
        state,
    )
    assert output['decision'] == 'block' and 'opus' in output['reason']
    failed = agent_payload('PostToolUse', result='FAKE:ERROR')
    output, record = run_post(failed, config, FakeVerifier(), state)
    assert output is None and record['status'] == 'verifier_failed'


def test_pin_modes(tmp_path):
    state = State(str(tmp_path))
    if_unset = settings(route_pin_cheap='if_unset')
    output, _ = run_pre(
        agent_payload('PreToolUse', model=None), if_unset, state
    )
    assert updated_model(output) == 'haiku'
    output, record = run_pre(
        agent_payload('PreToolUse', model='sonnet'), if_unset, state
    )
    assert output is None and record['status'] == 'unchanged'
    output, record = run_pre(
        agent_payload('PreToolUse', model='haiku'), settings(), state
    )
    assert output is None and record['changed'] is False


def with_prompt(prompt, model='sonnet'):
    payload = agent_payload('PreToolUse', model=model)
    payload['tool_input']['prompt'] = prompt
    return payload


def test_pre_mode_routes_from_the_task_alone(tmp_path):
    state = State(str(tmp_path))
    config = settings(route_mode='pre', route_pre_threshold=0.5)
    verifier = FakeVerifier()
    output, record = run_pre(
        with_prompt('prove it [[ACCEPT=0.2]]'), config, state, verifier
    )
    assert updated_model(output) == 'opus'
    assert record['source'] == 'pre_route'
    assert record['decision'] == 'escalate'
    assert verifier.calls[-1]['kind'] == 'pre'
    assert verifier.calls[-1]['candidate'] is None
    output, record = run_pre(
        with_prompt('add 2 and 2 [[ACCEPT=0.9]]'), config, state, verifier
    )
    assert updated_model(output) == 'haiku'
    assert record['decision'] == 'accept'
    # fail-open: the cheap model
    output, record = run_pre(
        with_prompt('FAKE:ERROR'), config, state, verifier
    )
    assert updated_model(output) == 'haiku'
    assert record['status'] == 'verifier_failed'
    # pre mode: no answer check
    output, record = run_post(
        agent_payload('PostToolUse'), config, verifier, state
    )
    assert output is None and record['status'] == 'mode_pre'
    # post mode: pin the cheap model without a verifier call
    output, record = run_pre(
        agent_payload('PreToolUse'), settings(), state, verifier
    )
    assert record['source'] == 'pin_cheap' and len(verifier.calls) == 3


def test_both_mode_checks_only_what_went_to_the_cheap_model(tmp_path):
    state, config = State(str(tmp_path)), settings(route_mode='both')
    verifier = FakeVerifier()
    output, record = run_post(
        agent_payload('PostToolUse', model='haiku'), config, verifier, state
    )
    assert record['decision'] == 'escalate' and len(verifier.calls) == 1
    # the pending re-run wins
    output, record = run_pre(
        agent_payload('PreToolUse', model='haiku'), config, state, verifier
    )
    assert updated_model(output) == 'opus'
    assert record['source'] == 'escalation'
    strong = agent_payload('PostToolUse', model='opus')
    strong['tool_response']['resolvedModel'] = 'claude-opus-5-5'
    output, record = run_post(strong, config, verifier, state)
    assert output is None and record['reason'] == 'already_strong'
    assert len(verifier.calls) == 1  # no second verifier call
    with pytest.raises(ConfigError):
        settings(route_mode='sometimes')


def test_tiers_mode_picks_one_of_three_models(tmp_path):
    state, verifier = State(str(tmp_path)), FakeVerifier()
    config = settings(
        route_mode='tiers', route_tier_low=0.4, route_tier_high=0.8
    )
    cases = (
        (0.9, 'haiku', 'cheap'),
        (0.5, 'sonnet', 'mid'),
        (0.1, 'opus', 'strong'),
    )
    for p_easy, model, tier in cases:
        # fake verifier: level = 1 - p
        output, record = run_pre(
            with_prompt(f'task [[ACCEPT={p_easy}]]'),
            config,
            state,
            verifier,
        )
        assert record['tier'] == tier and record['source'] == 'tier_route'
        assert verifier.calls[-1]['kind'] == 'tier'
        assert updated_model(output, 'sonnet') == model
    output, record = run_post(
        agent_payload('PostToolUse', model='haiku'), config, verifier, state
    )
    assert output is None and record['status'] == 'mode_tiers'
    checked = settings(route_mode='tiers', route_tier_check='true')
    output, record = run_post(
        agent_payload('PostToolUse', model='haiku'),
        checked,
        verifier,
        state,
    )
    assert record['decision'] == 'escalate'
    output, record = run_post(
        agent_payload('PostToolUse', model='sonnet'),
        checked,
        verifier,
        state,
    )
    assert output is None and record['reason'] == 'not_cheap'
    with pytest.raises(ConfigError):
        settings(route_mode='tiers', route_tier_low=0.9, route_tier_high=0.2)


def write_transcript(
    path,
    user='Fix the failing test in foo.py',
    assistant='Done, all tests pass.',
):
    def message(role, content):
        return {'type': role, 'message': {'role': role, 'content': content}}

    lines = [
        message('user', user),
        message('assistant', [{'type': 'text', 'text': 'working'}]),
        message('user', [{'type': 'tool_result', 'content': 'x'}]),
        message('assistant', [{'type': 'text', 'text': assistant}]),
    ]
    text = '\n'.join(json.dumps(line) for line in lines) + '\n'
    path.write_text(text, encoding='utf-8')
    return str(path)


def test_transcript_reader_skips_tool_results():
    with tempfile.TemporaryDirectory() as directory:
        path = write_transcript(Path(directory) / 't.jsonl')
        assert route_hooks.transcript_messages(path) == (
            'Fix the failing test in foo.py',
            'Done, all tests pass.',
        )
    assert route_hooks.transcript_messages('/nonexistent') == (None, None)


def stop_payload(
    transcript,
    last='Done [[ACCEPT=0.1]]',
    active=False,
    session='s2',
):
    return {
        'session_id': session,
        'hook_event_name': 'Stop',
        'transcript_path': transcript,
        'stop_hook_active': active,
        'last_assistant_message': last,
    }


def test_stop_blocks_once_then_allows(tmp_path):
    state, config = State(str(tmp_path / 'st')), settings()
    verifier = FakeVerifier()
    transcript = write_transcript(tmp_path / 't.jsonl')
    output, record = run_stop(
        stop_payload(transcript), config, verifier, state
    )
    assert output['decision'] == 'block'
    assert 'P(complete)=0.1' in output['reason']
    assert record['p_accept'] == 0.1
    assert verifier.calls[0]['kind'] == 'completion'
    assert verifier.calls[0]['task'] == 'Fix the failing test in foo.py'
    output, record = run_stop(
        stop_payload(transcript, active=True), config, verifier, state
    )
    assert output is None and record['status'] == 'max_blocks'
    assert len(verifier.calls) == 1


def test_stop_chain_guard_when_the_reason_becomes_a_user_message(tmp_path):
    state, config = State(str(tmp_path / 'st')), settings()
    first = write_transcript(tmp_path / 'a.jsonl')
    run_stop(stop_payload(first), config, FakeVerifier(), state)
    second = write_transcript(
        tmp_path / 'b.jsonl',
        user='[davi-verify] An automated completion check ...',
    )
    output, record = run_stop(
        stop_payload(second, active=True), config, FakeVerifier(), state
    )
    assert output is None and record['status'] == 'max_blocks'
    # a new user turn
    output, _ = run_stop(
        stop_payload(second, active=False), config, FakeVerifier(), state
    )
    assert output is not None


def test_stop_allows_complete_tasks_and_fails_open(tmp_path):
    state, config = State(str(tmp_path / 'st')), settings()
    transcript = write_transcript(tmp_path / 't.jsonl')
    done = stop_payload(transcript, last='done [[ACCEPT=0.9]]')
    assert run_stop(done, config, FakeVerifier(), state)[0] is None
    failed = stop_payload(transcript, last='FAKE:ERROR')
    output, record = run_stop(failed, config, FakeVerifier(), state)
    assert output is None and record['decision'] == 'allow'


def test_probe_modes(tmp_path):
    state = State(str(tmp_path))
    config = settings(probe='true', probe_nonce='N1', probe_pre_model='haiku')
    output, record = run_post(
        agent_payload('PostToolUse'), config, None, state
    )
    context = output['hookSpecificOutput']['additionalContext']
    assert 'DAVI-PROBE-A-N1' in context and record['tool_response_shape']
    output, record = run_pre(
        agent_payload('PreToolUse', model='opus'), config, state
    )
    assert updated_model(output) == 'haiku'
    assert record['model_requested'] == 'opus'
    transcript = write_transcript(tmp_path / 't.jsonl')
    output, _ = run_stop(stop_payload(transcript), config, None, state)
    assert 'DAVI-PROBE-B-N1' in output['reason']
    output, record = run_stop(
        stop_payload(transcript, active=True), config, None, state
    )
    assert output is None and record['decision'] == 'allow'


def test_main_end_to_end_with_log(tmp_path):
    log = tmp_path / 'h.jsonl'
    env = {
        'DAVI_ROUTE_ROUTE_ENABLED': 'true',
        'DAVI_ROUTE_LOG_FILE': str(log),
        'CLAUDE_PLUGIN_DATA': str(tmp_path),
    }
    out = io.StringIO()
    route_hooks.main(
        'post',
        stdin=io.StringIO(json.dumps(agent_payload('PostToolUse'))),
        stdout=out,
        stderr=io.StringIO(),
        env=env,
        verifier=FakeVerifier(),
    )
    output = json.loads(out.getvalue())
    assert output['hookSpecificOutput']['additionalContext']
    out = io.StringIO()
    route_hooks.main(
        'pre',
        stdin=io.StringIO(json.dumps(agent_payload('PreToolUse'))),
        stdout=out,
        stderr=io.StringIO(),
        env=env,
    )
    assert updated_model(json.loads(out.getvalue())) == 'opus'
    summary = harness.hooklog_summary(str(log))
    assert summary['by_kind']['post']['decision'] == {'escalate': 1}
    assert summary['by_kind']['pre']['model_set'] == {
        'haiku -> opus (escalation)': 1,
    }


def test_main_is_silent_when_off_or_other_tool_or_bad_config():
    on = {'DAVI_ROUTE_ROUTE_ENABLED': 'true'}
    cases = [
        ({}, agent_payload('PostToolUse')),
        (on, {**agent_payload('PostToolUse'), 'tool_name': 'Bash'}),
        (
            {**on, 'DAVI_ROUTE_ROUTE_THRESHOLD': '7'},
            agent_payload('PostToolUse'),
        ),
    ]
    for env, payload in cases:
        out = io.StringIO()
        route_hooks.main(
            'post',
            stdin=io.StringIO(json.dumps(payload)),
            stdout=out,
            stderr=io.StringIO(),
            env=env,
            verifier=FakeVerifier(),
        )
        assert out.getvalue() == ''


def run_launcher(kind, payload, env_extra, tmp_path):
    prefixes = ('DAVI_ROUTE_', 'CLAUDE_PLUGIN_OPTION_')
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(prefixes)
    }
    env.update({'CLAUDE_PLUGIN_DATA': str(tmp_path), **env_extra})
    return subprocess.run(
        [sys.executable, LAUNCHER, kind],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_launcher_fast_path_and_probe(tmp_path):
    payload = agent_payload('PostToolUse')
    result = run_launcher('post', payload, {}, tmp_path)
    assert result.returncode == 0
    assert result.stdout == '' and result.stderr == ''
    probe_env = {'DAVI_ROUTE_PROBE': '1', 'DAVI_ROUTE_PROBE_NONCE': 'L'}
    result = run_launcher('post', payload, probe_env, tmp_path)
    context = json.loads(result.stdout)['hookSpecificOutput'][
        'additionalContext'
    ]
    assert result.returncode == 0 and 'DAVI-PROBE-A-L' in context
    jev_env = {
        'DAVI_ROUTE_ROUTE_ENABLED': 'true',
        'DAVI_ROUTE_ROUTE_JUDGE': 'jev',
    }
    result = run_launcher('post', payload, jev_env, tmp_path)
    # no key: fail-open, logged
    assert result.returncode == 0 and result.stdout == ''
    last = json.loads(result.stderr.strip().splitlines()[-1])
    assert last['outcome'] == 'local_config'


def test_launcher_finds_src_in_the_marketplace_clone(tmp_path):
    # a marketplace install: only plugins/davi-route is copied to the cache;
    # the marketplace itself is a full clone of the repository
    base = tmp_path / 'plugins'
    hooks_dir = base / 'cache' / 'datumo' / 'davi-route' / '0.5.0' / 'hooks'
    hooks_dir.mkdir(parents=True)
    launcher = hooks_dir / 'route_hook.py'
    shutil.copy(LAUNCHER, launcher)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('DAVI_ROUTE_', 'CLAUDE_PLUGIN_OPTION_'))
    }
    env.update(
        {
            'CLAUDE_PLUGIN_DATA': str(tmp_path / 'data'),
            'DAVI_ROUTE_PROBE': '1',
            'DAVI_ROUTE_PROBE_NONCE': 'M',
        }
    )

    def run():
        return subprocess.run(
            [sys.executable, str(launcher), 'post'],
            input=json.dumps(agent_payload('PostToolUse')),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def probe_ran(result):
        output = json.loads(result.stdout)['hookSpecificOutput']
        return 'DAVI-PROBE-A-M' in output['additionalContext']

    result = run()
    assert result.returncode == 0 and result.stdout == ''
    last = json.loads(result.stderr.strip().splitlines()[-1])
    assert last['status'] == 'src_missing'
    # a git or GitHub marketplace: cloned to <plugins>/marketplaces/<name>
    clone = base / 'marketplaces' / 'datumo'
    clone.mkdir(parents=True)
    (clone / 'src').symlink_to(ROOT / 'src', target_is_directory=True)
    assert probe_ran(run())
    # a directory marketplace: installLocation is the checkout itself
    shutil.rmtree(base / 'marketplaces')
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    (checkout / 'src').symlink_to(ROOT / 'src', target_is_directory=True)
    known = {'datumo': {'installLocation': str(checkout)}}
    (base / 'known_marketplaces.json').write_text(json.dumps(known))
    assert probe_ran(run())


def load_launcher():
    spec = importlib.util.spec_from_file_location('route_hook_test', LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_bundle_is_current():
    stale = bundle.Bundle().stale()
    assert not stale, f'run `davi-route bundle` (stale: {stale})'


def test_installed_copy_runs_on_its_bundle_alone(tmp_path):
    # what a marketplace install leaves: plugins/davi-route only, no checkout
    # above it, no marketplace clone. -S skips site (and the editable install
    # of judge_adapter); httpx comes from the pydeps folder setup installs.
    installed = tmp_path / 'cache' / 'datumo' / 'davi-route' / '0.5.1'
    shutil.copytree(PLUGIN, installed)
    data = tmp_path / 'data'
    data.mkdir()
    purelib = sysconfig.get_paths()['purelib']
    (data / 'pydeps').symlink_to(purelib, target_is_directory=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('DAVI_ROUTE_', 'CLAUDE_PLUGIN_OPTION_'))
        and key != 'PYTHONPATH'
    }
    env.update(
        {
            'CLAUDE_PLUGIN_DATA': str(data),
            'DAVI_ROUTE_ROUTE_ENABLED': 'true',
            'DAVI_ROUTE_ROUTE_JUDGE': 'fake',
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            '-S',
            str(installed / 'hooks' / 'route_hook.py'),
            'post',
        ],
        input=json.dumps(agent_payload('PostToolUse')),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)['hookSpecificOutput']
    assert 'opus' in output['additionalContext']


def test_setup_installs_httpx_with_pip_target(tmp_path):
    launcher = load_launcher()
    deps = str(tmp_path / 'data' / 'pydeps')
    calls = []

    def refuse_then_install(command, **_):
        calls.append(command)
        code = 1 if len(calls) == 1 else 0
        return subprocess.CompletedProcess(
            command, code, '', 'error: externally-managed-environment'
        )

    status, detail = launcher.install_dependency(
        deps, run=refuse_then_install, python='py'
    )
    assert status == 'installed' and deps in detail
    assert calls[0][:4] == ['py', '-m', 'pip', 'install']
    assert calls[0][calls[0].index('--target') + 1] == deps
    assert '--break-system-packages' in calls[1]
    assert not os.path.exists(deps + '.lock')
    os.mkdir(deps + '.lock')
    status, _ = launcher.install_dependency(deps, run=refuse_then_install)
    assert status == 'install_busy' and len(calls) == 2


def test_setup_runs_only_when_a_feature_is_on(monkeypatch):
    launcher = load_launcher()
    for key in list(os.environ):
        if key.startswith(('DAVI_ROUTE_', 'CLAUDE_PLUGIN_OPTION_')):
            monkeypatch.delenv(key)
    assert not launcher.feature_on('setup')
    monkeypatch.setenv('CLAUDE_PLUGIN_OPTION_STOP_VERIFY_ENABLED', 'true')
    assert launcher.feature_on('setup')
    monkeypatch.setenv('CLAUDE_PLUGIN_OPTION_AUTO_INSTALL_DEPS', 'false')
    assert not launcher.feature_on('setup')
    monkeypatch.setenv('DAVI_ROUTE_PROBE', '1')
    assert not launcher.feature_on('setup') and launcher.feature_on('post')


def load_configure():
    path = PLUGIN / 'hooks' / 'configure.py'
    spec = importlib.util.spec_from_file_location('configure_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_options_file_fills_unset_variables(tmp_path, monkeypatch):
    launcher = load_launcher()
    path = tmp_path / 'davi-route.json'
    options = {
        'route_enabled': True,
        'route_mode': 'post',
        'route_agent_types': ['a', 'b'],
        'Bad-Key': 'x',
        'log_file': None,
    }
    path.write_text(json.dumps(options))
    environ = {'DAVI_ROUTE_CONFIG': str(path), 'DAVI_ROUTE_ROUTE_MODE': 'pre'}
    monkeypatch.setattr(launcher.os, 'environ', environ)
    launcher.load_config_file()
    assert environ['DAVI_ROUTE_ROUTE_ENABLED'] == 'true'
    assert environ['DAVI_ROUTE_ROUTE_MODE'] == 'pre'  # the variable wins
    assert environ['DAVI_ROUTE_ROUTE_AGENT_TYPES'] == 'a,b'
    assert 'DAVI_ROUTE_LOG_FILE' not in environ
    assert not any('BAD' in name for name in environ)
    assert launcher.feature_on('post')
    path.write_text('{not json')
    assert launcher.read_config(str(path)) == {}
    home = {'CLAUDE_CONFIG_DIR': str(tmp_path / 'cfg')}
    monkeypatch.setattr(launcher.os, 'environ', home)
    assert launcher.config_path() == str(tmp_path / 'cfg' / 'davi-route.json')


def test_setup_command_writes_merges_and_checks(tmp_path, monkeypatch):
    configure = load_configure()
    path = tmp_path / 'davi-route.json'
    monkeypatch.setenv('DAVI_ROUTE_CONFIG', str(path))
    monkeypatch.delenv('DAVI_ROUTE_ROUTE_MODE', raising=False)
    checked = []

    def check(url, key=None):
        checked.append((url, key))
        return True, 'ok'

    def run(*argv):
        out = io.StringIO()
        code = configure.main(list(argv), check=check, out=out)
        return code, out.getvalue()

    code, text = run('"http://davi.test:20100/"')
    assert code == 0 and 'Davi server http://davi.test:20100: ok' in text
    assert json.loads(path.read_text()) == {
        'davi_base_url': 'http://davi.test:20100',
        'route_enabled': True,
        'route_judge': 'davi',
        'route_mode': 'tiers',
    }
    assert oct(path.stat().st_mode & 0o777) == '0o600'
    code, _ = run(
        '--mode',
        'post',
        '--set',
        'log_file=/tmp/r.jsonl',
        '--set',
        'davi_api_key=secret-0000',
        '--set',
        'route_threshold=0.4',
    )
    saved = json.loads(path.read_text())
    assert saved['route_mode'] == 'post' and saved['route_threshold'] == 0.4
    assert saved['davi_base_url'] == 'http://davi.test:20100'
    assert len(checked) == 1  # no URL given: no server check
    code, text = run('--show')
    assert 'secret-0000' not in text and '***' in text
    assert checked[-1] == ('http://davi.test:20100', 'secret-0000')
    code, _ = run('--off')
    saved = json.loads(path.read_text())
    assert saved['route_enabled'] is False
    assert saved['stop_verify_enabled'] is False
    code, text = run('davi.test:20100')
    assert code == 2 and 'http://' in text
    code, text = run('--set', 'no-equals')
    assert code == 2


def test_setup_command_checks_a_real_http_server():
    import http.server
    import threading

    configure = load_configure()
    replies = {
        '/v1/systemone': (
            200,
            {'answers': {'route': {'probabilities': {'accept': 0.97}}}},
        )
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server's naming
            self.rfile.read(int(self.headers['Content-Length']))
            code, body = replies.get(self.path, (404, {}))
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        ok, message = configure.check_server(url, timeout=5)
        assert ok and 'P(accept)=0.970' in message
        ok, message = configure.check_server(url + '/nope', timeout=5)
        assert not ok and 'HTTP 404' in message
    finally:
        server.shutdown()
    ok, _ = configure.check_server('http://127.0.0.1:9', timeout=2)
    assert not ok


def test_setup_skill_runs_the_script_without_shell_preexecution():
    # a skill (not commands/ with a `!` line): claude.ai-synced copies do not
    # run `!` lines, and the Chat "+" menu lists skills
    assert not (PLUGIN / 'commands').exists()
    text = (PLUGIN / 'skills' / 'setup' / 'SKILL.md').read_text('utf-8')
    front, _, body = text.partition('\n---\n')
    assert 'name: setup' in front
    assert 'allowed-tools: Bash(python3:*)' in front
    assert 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/configure.py"' in body
    assert '!`' not in text


def test_marketplace_lists_the_plugin():
    path = ROOT / '.claude-plugin' / 'marketplace.json'
    market = json.loads(path.read_text(encoding='utf-8'))
    entries = {entry['name']: entry for entry in market['plugins']}
    manifest = PLUGIN / '.claude-plugin' / 'plugin.json'
    plugin = json.loads(manifest.read_text(encoding='utf-8'))
    assert entries[plugin['name']]['source'] == './plugins/davi-route'


def test_plugin_manifest_matches_settings():
    manifest = PLUGIN / '.claude-plugin' / 'plugin.json'
    plugin = json.loads(manifest.read_text(encoding='utf-8'))
    hooks_file = PLUGIN / 'hooks' / 'hooks.json'
    hooks = json.loads(hooks_file.read_text(encoding='utf-8'))['hooks']
    assert set(hooks) == {
        'SessionStart',
        'PreToolUse',
        'PostToolUse',
        'Stop',
        'SubagentStop',
    }
    fields = set(Settings.__dataclass_fields__)
    # judge options read by verifiers.build_verifier, not by Settings
    secrets = {
        'davi_api_key',
        'runpod_api_key',
        'choice_endpoint_id',
        'typesafe_api_key',
        'anthropic_api_key',
    }
    judge_options = {'davi_base_url', 'usd_per_second'}
    config = plugin['userConfig']
    assert set(config) <= fields | secrets | judge_options
    assert all(config[key]['sensitive'] for key in secrets)
    assert not any(config[key].get('sensitive') for key in judge_options)
    defaults = Settings()
    for key, spec in config.items():
        if key in fields and 'default' in spec:
            assert spec['default'] == getattr(defaults, key), key
    for entries in hooks.values():
        for entry in entries:
            (hook,) = entry['hooks']
            assert hook['args'][0] == LAUNCHER_ARG
            # setup (SessionStart) is handled by the launcher alone
            kinds = (*route_hooks.HOOK_KINDS, 'setup')
            assert hook['args'][1] in kinds
            assert hook['timeout'] > defaults.wall_budget_seconds


# --- grading ----------------------------------------------------------------
def test_graders():
    task = harness.Task('a', 'q', answer='391', grader='number')
    assert grading.grade(task, 'The product is 391.')[0] is True
    assert grading.grade(task, '17*23=390')[0] is False
    paris = harness.Task('b', 'q', answer='Paris')
    assert grading.grade(paris, 'It is paris.')[0] is True
    regex = harness.Task('c', 'q', answer='^ok$', grader='regex')
    assert grading.grade(regex, 'OK')[0] is True
    ungraded = harness.Task('d', 'q')
    assert grading.grade(ungraded, 'anything') == (None, 'ungraded')
    command = harness.Task('e', 'q', check_cmd='grep -q 42')
    assert grading.grade(command, 'x 42 y')[0] is True
    assert grading.grade(harness.Task('f', 'q', answer='x'), '   ')[0] is False


def test_graders_ignore_markdown_emphasis_and_read_korean_amounts():
    choice = harness.Task(
        'a', 'q', answer=r'정답\s*[:：]\s*\(?B\b', grader='regex'
    )
    assert grading.grade(choice, '풀이...\n정답: **B**')[0] is True
    assert grading.grade(choice, '정답: **C**')[0] is False
    money = harness.Task('b', 'q', answer='2130000000', grader='number')
    assert grading.grade(money, '정답: 21억 3,000만 원')[0] is True
    assert grading.grade(money, '정답: 21억 원')[0] is False
    two = harness.Task('c', 'q', answer='2', grader='number')
    assert grading.grade(two, '정답: **3**')[0] is False
    assert grading.last_korean_amount('약 1.5만 명') == 15000.0
    assert grading.last_korean_amount('만약 그렇다면') is None


def test_grader_classes():
    assert isinstance(grading.grader_for('number'), grading.ReferenceGrader)
    assert isinstance(grading.grader_for('none'), grading.NoGrader)
    with pytest.raises(TypeError):
        grading.ReferenceGrader()


def test_llm_grader_reads_the_last_yes_or_no():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        text = 'The reference says yes but the response differs.\n\nno'
        return httpx.Response(
            200,
            json={
                'content': [{'type': 'text', 'text': text}],
            },
        )

    task = harness.Task('a', 'q', answer='391', grader='llm')
    env = {'ANTHROPIC_API_KEY': 'sk-test-0000000000'}
    result = grading.grade(
        task, '390', env=env, transport=httpx.MockTransport(handler)
    )
    assert result == (False, 'llm:no')
    assert seen[0]['model'] == grading.GRADER_DEFAULT_MODEL
    correct, detail = grading.grade(task, '391', env={})
    assert correct is None and detail.startswith('llm grader error')


# --- harness ----------------------------------------------------------------
def test_claude_cmd_and_result_parsing():
    arm = harness.parse_arm('plug=sonnet:--plugin-dir plugins/davi-route')
    command = harness.claude_cmd(
        harness.Task('a', 'hello'),
        arm,
        max_turns=3,
        budget_usd=0.5,
        session_id='u',
    )
    assert command[:5] == ['claude', '-p', 'hello', '--model', 'sonnet']
    assert command[-2:] == ['--plugin-dir', 'plugins/davi-route']
    assert '--max-budget-usd' in command and 'u' in command
    result = harness.parse_result(
        'noise\n{"type":"result","result":"OK","total_cost_usd":0.01}\n'
    )
    assert result['result'] == 'OK'
    env = harness.child_env(arm, {'CLAUDE_CODE_SESSION_ID': 'x', 'A': '1'})
    assert 'CLAUDE_CODE_SESSION_ID' not in env


def fake_runner(answers):
    """answers: (prompt, model) -> (text, cost)."""

    def run(command, env, cwd, timeout):
        prompt = command[2]
        model = command[command.index('--model') + 1]
        text, cost = answers[(prompt, model)]
        return {
            'exit_code': 0,
            'wall_s': 1.0,
            'result': text,
            'is_error': False,
            'total_cost_usd': cost,
            'duration_ms': 1000,
            'num_turns': 1,
            'session_id': 's',
            'model_usage': {model: {'costUSD': cost}},
            'subagents': None,
            'permission_denials': 0,
            'stderr_tail': None,
        }

    return run


def build_cache(tmp_path, n=40):
    """Cheap answers are correct on even ids, strong ones always. The fake
    P(accept) ranks perfectly except for one confidently wrong answer."""
    tasks, answers = [], {}
    for i in range(n):
        task = harness.Task(f't{i}', f'q{i}', answer='yes')
        tasks.append(task)
        cheap_ok = i % 2 == 0
        p = 0.9 if cheap_ok else 0.1
        if i == 1:
            p = 0.95
        text = ('yes' if cheap_ok else 'no') + f' [[ACCEPT={p}]]'
        answers[(task.prompt, 'haiku')] = (text, 0.01)
        answers[(task.prompt, 'opus')] = ('yes', 0.10)
    cache = str(tmp_path / 'cache')
    arms = [harness.Arm('cheap', 'haiku'), harness.Arm('strong', 'opus')]
    harness.generate(
        tasks, arms, cache, workers=1, runner=fake_runner(answers), log=quiet
    )
    # all cached: the runner is not called again
    harness.generate(tasks, arms, cache, workers=1, runner=None, log=quiet)
    harness.judge(
        tasks,
        cache,
        'cheap',
        ['fake'],
        log=quiet,
        verifier_factory=lambda name: FakeVerifier(cost_usd=0.0001),
    )
    return tasks, cache


def test_run_cache_keeps_the_newest_record(tmp_path):
    store = harness.RunCache(str(tmp_path))
    store.add_run({'task_id': 'a', 'arm': 'x', 'correct': False})
    store.add_run({'task_id': 'a', 'arm': 'x', 'correct': True})
    assert store.runs() == {
        ('a', 'x'): {'task_id': 'a', 'arm': 'x', 'correct': True},
    }
    assert store.judgments() == {}


def test_pre_judging_and_its_cost(tmp_path):
    tasks, cache = build_cache(tmp_path)
    marked = [
        harness.Task(
            task.id,
            task.prompt
            + (
                ' [[ACCEPT=0.9]]'
                if int(task.id[1:]) % 2 == 0
                else ' [[ACCEPT=0.1]]'
            ),
        )
        for task in tasks
    ]
    harness.judge(
        marked,
        cache,
        'cheap',
        ['fake'],
        kind='pre',
        log=quiet,
        verifier_factory=lambda name: FakeVerifier(),
    )
    rows, judges = analysis.demo_rows(cache, 'cheap', 'strong')
    assert judges == ['fake', 'fake:pre']
    odd = next(row for row in rows if row['id'] == 't3')
    assert odd['judges']['fake:pre']['route'] == 'escalate'
    # strong only: the cheap model never ran
    assert odd['judges']['fake:pre']['cost'] == pytest.approx(0.10)
    expected = 0.01 + 0.0001 + 0.10
    assert odd['judges']['fake']['cost'] == pytest.approx(expected)
    report = analysis.analyze(cache, 'cheap', 'strong', calib_frac=0.5, seed=1)
    pre = report['judges']['fake:pre']
    assert pre['baselines']['always_strong']['accuracy'] == 1.0


# --- analysis ---------------------------------------------------------------
def test_analyze_baselines_auroc_and_frozen_threshold(tmp_path):
    _, cache = build_cache(tmp_path)
    out_dir = tmp_path / 'r'
    report = analysis.analyze(
        cache,
        'cheap',
        'strong',
        calib_frac=0.5,
        seed=1,
        tolerance=0.1,
        out_dir=str(out_dir),
    )
    base = report['baselines']
    assert report['n_paired'] == 40
    assert base['always_cheap']['accuracy'] == 0.5
    assert base['always_strong']['accuracy'] == 1.0
    assert base['oracle']['accuracy'] == 1.0
    oracle_cost = 0.01 + 0.5 * 0.10
    assert base['oracle']['cost_per_task'] == pytest.approx(oracle_cost)
    judge = report['judges']['fake']
    # one negative outranks all 20 positives
    assert judge['auroc_cheap_correct'] == pytest.approx(1 - 20 / 400)
    frozen = judge['frozen']
    random_accuracy = frozen['test_random_same_rate']['accuracy']
    assert frozen['test']['accuracy'] >= random_accuracy
    assert frozen['cost_vs_strong'] < 1.0
    markdown = (out_dir / 'report.md').read_text(encoding='utf-8')
    assert '| fake |' in markdown and 'always_strong' in markdown
    curve = (out_dir / 'curve.csv').read_text()
    assert curve.startswith('judge,threshold')
    cost_per_success = report['arms']['cheap']['cost_per_success']
    assert cost_per_success == pytest.approx(0.4 / 20)


def test_sweep_endpoints_and_random_baseline():
    rows = [
        analysis.ReplayRow(
            str(i),
            cheap_ok=i % 2 == 0,
            strong_ok=True,
            cheap_cost=1.0,
            strong_cost=10.0,
            p=0.1 * i,
            judge_cost=0.0,
        )
        for i in range(10)
    ]
    points = analysis.sweep(rows)
    assert points[0]['escalation_rate'] == 0.0
    assert points[-1]['escalation_rate'] == 1.0
    assert points[-1]['cost_per_task'] == pytest.approx(11.0)
    rand = analysis.random_point(rows, 0.5)
    assert rand['accuracy'] == pytest.approx(0.75)
    assert rand['cost_per_task'] == pytest.approx(6.0)
    assert analysis.auroc([0.9, 0.8, 0.1], [True, True, False]) == 1.0
    assert analysis.auroc([0.5], [True]) is None


def test_replay_row_costs():
    row = analysis.ReplayRow(
        'a',
        cheap_ok=False,
        strong_ok=True,
        cheap_cost=1.0,
        strong_cost=10.0,
        p=0.2,
        judge_cost=0.5,
    )
    assert row.escalates(0.5) and not row.escalates(0.1)
    assert row.cost(True) == 11.5 and row.cost(False) == 1.5
    pre = analysis.ReplayRow('b', True, True, 1.0, 10.0, p=0.2, pre=True)
    assert pre.cost(True) == 10.0 and pre.correct(False) is True
    failed = analysis.ReplayRow('c', True, False, 1.0, 10.0)
    assert not failed.escalates(0.5)
    assert failed.escalates(0.5, on_error='escalate')


class AlwaysAccept(FakeVerifier):
    def _verify(self, task, candidate, rubric, kind, started):
        return Verdict(self.name, kind, 0.9, 'ok', 0.0, 0.0002, 'const')


def test_demo_rows_curate_and_regrade(tmp_path, capsys):
    tasks, cache = build_cache(tmp_path)
    harness.judge(
        tasks,
        cache,
        'cheap',
        ['rival'],
        log=quiet,
        verifier_factory=lambda name: AlwaysAccept(name='rival'),
    )
    rows, judges = analysis.demo_rows(cache, 'cheap', 'strong', threshold=0.5)
    assert judges == ['fake', 'rival'] and len(rows) == 40
    t1 = next(row for row in rows if row['id'] == 't1')
    assert t1['ideal'] == 'escalate'
    assert t1['judges']['fake']['route'] == 'accept'
    assert not t1['judges']['fake']['correct']
    t3 = next(row for row in rows if row['id'] == 't3')
    expected = 0.01 + 0.0001 + 0.10
    assert t3['judges']['fake']['cost'] == pytest.approx(expected)
    summary = analysis.demo_summary(rows, judges)
    assert summary['always_cheap']['accuracy'] == 0.5
    assert summary['fake']['accuracy'] == pytest.approx(39 / 40)
    assert summary['rival']['strong_rate'] == 0.0
    assert summary['fake']['right_route'] == pytest.approx(39 / 40)

    selection = analysis.curate(
        cache, 'cheap', 'strong', ours='fake', n=10, accept_frac=0.5
    )
    # the rival always accepts, so it is never wrong on accept tasks:
    # every pick is an escalate task, t1 excluded
    assert selection['picked'] == {'accept': 0, 'escalate': 10}
    assert selection['available'] == {'accept': 0, 'escalate': 19}
    assert 't1' not in selection['selected']
    assert 'not an estimate' in selection['note']
    with pytest.raises(ValueError):
        analysis.curate(cache, 'cheap', 'strong', ours='davi')

    task_file = tmp_path / 't.jsonl'
    task_file.write_text(
        ''.join(
            json.dumps({'id': t.id, 'prompt': t.prompt, 'answer': t.answer})
            + '\n'
            for t in tasks
        ),
        encoding='utf-8',
    )
    out = tmp_path / 'demo' / 'demo.tasks.jsonl'
    assert (
        cli.main(
            [
                'curate',
                '--tasks',
                str(task_file),
                '--cache',
                cache,
                '--ours',
                'fake',
                '--n',
                '4',
                '--out',
                str(out),
            ]
        )
        == 0
    )
    assert len(out.read_text(encoding='utf-8').splitlines()) == 4
    criteria = tmp_path / 'demo' / 'demo.tasks.selection.json'
    assert json.loads(criteria.read_text())['ours'] == 'fake'
    assert cli.main(['demo', '--cache', cache, '--tasks', str(out)]) == 0
    text = capsys.readouterr().out
    assert 'always_strong' in text and 'rival' in text

    # every grade flips
    flipped = [harness.Task(t.id, t.prompt, answer='no') for t in tasks]
    assert harness.regrade(flipped, cache, log=quiet) == 80
    assert harness.regrade(flipped, cache, log=quiet) == 0
    rows, _ = analysis.demo_rows(cache, 'cheap', 'strong')
    assert sum(row['cheap_ok'] for row in rows) == 20
    assert not any(row['strong_ok'] for row in rows)


def test_cli_dry_run_and_analyze(tmp_path, capsys):
    task_file = tmp_path / 't.jsonl'
    task_file.write_text(
        json.dumps({'id': 'a', 'prompt': 'hi', 'answer': 'x'}) + '\n',
        encoding='utf-8',
    )
    args = [
        'generate',
        '--tasks',
        str(task_file),
        '--cache',
        str(tmp_path / 'c'),
        '--arm',
        'cheap=haiku',
    ]
    assert cli.main(args + ['--dry-run']) == 0
    assert 'claude -p hi --model haiku' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(args)
    _, cache = build_cache(tmp_path)
    assert (
        cli.main(
            [
                'analyze',
                '--cache',
                cache,
                '--cheap',
                'cheap',
                '--strong',
                'strong',
            ]
        )
        == 0
    )


# --- probes -----------------------------------------------------------------
def test_probe_evaluation():
    nonce = 'N'
    results = {
        'agent': {
            'run': {
                'result': f'391 DAVI-PROBE-A-{nonce}',
                'model_usage': {
                    'claude-sonnet-5-5': {'outputTokens': 50},
                    'claude-haiku-5-5': {'outputTokens': 5},
                },
                'subagents': {'spawned': 1},
            },
            'hooks': [{'kind': 'post', 'tool': 'Agent', 'model_in': 'haiku'}],
        },
        'stop': {
            'run': {'result': f'DAVI-PROBE-B-{nonce}', 'exit_code': 0},
            'hooks': [
                {'kind': 'stop', 'decision': 'block'},
                {'kind': 'stop', 'decision': 'allow'},
            ],
        },
        'pre': {
            'run': {
                'model_usage': {
                    'claude-sonnet-5-5': {'outputTokens': 9},
                    'claude-opus-5-5': {'outputTokens': 9},
                }
            },
            'hooks': [
                {
                    'kind': 'pre',
                    'changed': True,
                    'model_requested': 'opus',
                    'model_set': 'haiku',
                }
            ],
        },
    }
    got = {c['id']: c['result'] for c in probe.evaluate(results, nonce)}
    assert got == {
        '1a': 'pass',
        '1b': 'pass',
        '2': 'pass',
        '3a': 'pass',
        '3b': 'pass',
        '3c': 'pass',
        '4': 'fail',
    }


def launch(kind, env, payload):
    return subprocess.run(
        [sys.executable, LAUNCHER, kind],
        env=env,
        text=True,
        input=json.dumps(payload),
        capture_output=True,
    )


def test_probe_runner_with_fake_claude(tmp_path):
    """run_probes wiring: the fake runner plays the hooks by running the
    real launcher with the probe environment."""

    def runner(command, env, cwd, timeout):
        prompt = command[2]
        if 'READY' in prompt:
            for active in (False, True):
                launch(
                    'stop',
                    env,
                    {
                        'session_id': 'p',
                        'hook_event_name': 'Stop',
                        'transcript_path': '',
                        'stop_hook_active': active,
                        'last_assistant_message': 'READY',
                    },
                )
            nonce = env['DAVI_ROUTE_PROBE_NONCE']
            return {'result': f'DAVI-PROBE-B-{nonce}', 'exit_code': 0}
        model = 'opus' if 'model "opus"' in prompt else 'haiku'
        pre = launch('pre', env, agent_payload('PreToolUse', model=model))
        used = model
        if pre.stdout:
            used = updated_model(json.loads(pre.stdout))
        post = launch('post', env, agent_payload('PostToolUse', model=used))
        context = json.loads(post.stdout)['hookSpecificOutput'][
            'additionalContext'
        ]
        marker = context.split()[3].rstrip('.')
        return {
            'result': f'391 {marker}',
            'exit_code': 0,
            'subagents': {'spawned': 1},
            'model_usage': {
                'claude-sonnet-5-5': {'outputTokens': 9},
                f'claude-{used}-5-5': {'outputTokens': 3},
            },
        }

    report = probe.run_probes(
        str(PLUGIN),
        str(tmp_path / 'probe'),
        runner=runner,
        nonce='Z',
        log=quiet,
    )
    got = {c['id']: c['result'] for c in report['checks']}
    expected = ('1a', '1b', '2', '3a', '3b', '3c', '4')
    assert got == {key: 'pass' for key in expected}, report['checks']
    assert (tmp_path / 'probe' / 'probe-report.json').is_file()
