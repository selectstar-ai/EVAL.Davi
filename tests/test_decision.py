"""Decision core: questions, strict reading, errors, clients and settings.

Model-free: every HTTP call goes to an in-process MockTransport.
"""

from __future__ import annotations

import json
import math

import httpx
import pytest

from judge_adapter.decision import (
    ChoiceQuestion,
    DaviClient,
    DecisionError,
    JevClient,
    NoulQuestion,
    build_client,
    with_options_file,
)
from judge_adapter.decision import settings as decision_settings
from judge_adapter.routing.config import SECRET_ENVS

KEY = 'sk-test-secret-0123456789'
CHOICE = ChoiceQuestion(
    'Which document answers the query?', {'a': 'Doc a', 'b': 'Doc b'}
)
NOUL = NoulQuestion(
    'Does the passage answer the query?',
    {'true': 'it does', 'false': 'it does not'},
)


class Server:
    """A fake /v1/systemone that records requests and replies in turn."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self._requests = []

    @property
    def requests(self):
        return list(self._requests)

    def transport(self):
        def handle(request):
            self._requests.append(request)
            reply = self._replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            status, body = reply
            if isinstance(body, str):
                return httpx.Response(status, text=body)
            return httpx.Response(status, json=body)

        return httpx.MockTransport(handle)

    def body(self, index=0):
        return json.loads(self._requests[index].content)


def ok(answers, **extra):
    return 200, {'model': 'davi', 'answers': answers, 'usage': {}, **extra}


def davi(server, **kwargs):
    return DaviClient(
        'http://davi.test', transport=server.transport(), **kwargs
    )


def jev(server, key=KEY, **kwargs):
    return JevClient(lambda: key, transport=server.transport(), **kwargs)


# --- questions ---------------------------------------------------------------
@pytest.mark.parametrize(
    'make',
    [
        lambda: ChoiceQuestion('', {'a': 'x', 'b': 'y'}),
        lambda: ChoiceQuestion('pick', {'a': 'x'}),
        lambda: ChoiceQuestion('pick', ['a', 'b']),
        lambda: ChoiceQuestion('pick', {'a': 'x', 'b': ' '}),
        lambda: NoulQuestion('is it?', {'yes': 'y', 'no': 'n'}),
        lambda: NoulQuestion('is it?', {'true': 't', 'false': ''}),
        lambda: NoulQuestion('is it?', NOUL.criteria, threshold=1.5),
        lambda: NoulQuestion('is it?', NOUL.criteria, threshold=True),
        lambda: NoulQuestion('is it?', NOUL.criteria, threshold=math.nan),
    ],
)
def test_bad_questions_are_invalid_requests(make):
    with pytest.raises(DecisionError) as caught:
        make()
    assert caught.value.code == 'invalid_request'


def test_noul_is_sent_as_a_true_false_choice_or_natively():
    assert NOUL.wire('choice') == {
        'type': 'choice',
        'instructions': NOUL.instructions,
        'criteria': {'true': 'it does', 'false': 'it does not'},
    }
    assert NOUL.wire('native') == {
        'type': 'noul',
        'instructions': NOUL.instructions,
    }


# --- reading -----------------------------------------------------------------
def test_choice_reads_the_distribution_and_the_most_probable_option():
    server = Server(
        ok({'q': {'choice': 'a', 'probabilities': {'a': 0.7, 'b': 0.3}}})
    )
    answer = davi(server).decide('state', CHOICE)
    assert answer.value == 'a'
    assert answer.probabilities == {'a': 0.7, 'b': 0.3}
    assert server.body()['questions']['q']['criteria'] == CHOICE.options


def test_a_tie_goes_to_the_first_option_in_question_order():
    server = Server(ok({'q': {'probabilities': {'b': 0.5, 'a': 0.5}}}))
    assert davi(server).decide('state', CHOICE).value == 'a'


@pytest.mark.parametrize(
    'answer',
    [
        {'choice': 'b', 'probabilities': {'a': 0.7, 'b': 0.3}},
        {'probabilities': {'a': 0.7}},
        {'probabilities': {'a': 0.7, 'b': 0.3, 'c': 0.0}},
        {'probabilities': {'a': 0.7, 'b': 0.2}},
        {'probabilities': {'a': True, 'b': 0.0}},
        {'probabilities': {'a': 'NaN', 'b': 0.0}},
        {'probabilities': {'a': 1.5, 'b': -0.5}},
        {'probabilities': None},
        None,
    ],
)
def test_bad_choice_answers_are_format_errors(answer):
    server = Server(ok({'q': answer}))
    with pytest.raises(DecisionError) as caught:
        davi(server).decide('state', CHOICE)
    assert caught.value.code == 'format_error'


def test_sum_tolerance_accepts_float32_rounding():
    shares = {'a': 0.998737633228302, 'b': 0.001262335223145783}
    server = Server(ok({'q': {'probabilities': shares}}))
    assert davi(server).decide('state', CHOICE).value == 'a'


@pytest.mark.parametrize(
    'mode, answer, p_true',
    [
        ('choice', {'probabilities': {'true': 0.8, 'false': 0.2}}, 0.8),
        ('native', {'type': 'noul', 'noul': 0.8}, 0.8),
    ],
)
def test_noul_reads_p_true_in_both_modes(mode, answer, p_true):
    server = Server(ok({'q': answer}))
    result = davi(server, noul_mode=mode).decide('state', NOUL)
    assert result.p_true == p_true
    assert result.value is True
    assert result.mode == mode


def test_noul_threshold_boundary_is_inclusive():
    question = NoulQuestion('is it?', NOUL.criteria, threshold=0.8)
    server = Server(ok({'q': {'noul': 0.8}}), ok({'q': {'noul': 0.79}}))
    client = davi(server, noul_mode='native')
    assert client.decide('state', question).value is True
    assert client.decide('state', question).value is False


@pytest.mark.parametrize('answer', [{'noul': None}, {'noul': 2.0}, {}])
def test_bad_native_noul_answers_are_format_errors(answer):
    server = Server(ok({'q': answer}))
    with pytest.raises(DecisionError) as caught:
        davi(server, noul_mode='native').decide('state', NOUL)
    assert caught.value.code == 'format_error'


def test_several_questions_go_in_one_request_and_fail_together():
    server = Server(
        ok(
            {
                'c': {'probabilities': {'a': 0.9, 'b': 0.1}},
                'n': {'probabilities': {'true': 0.4, 'false': 0.6}},
            }
        ),
        ok({'c': {'probabilities': {'a': 0.9, 'b': 0.1}}}),
    )
    client = davi(server)
    reply = client.ask({'query': 'q'}, {'c': CHOICE, 'n': NOUL})
    assert reply.answers['c'].value == 'a'
    assert reply.answers['n'].value is False
    assert len(server.requests) == 1
    assert server.body()['state'] == {'query': 'q'}
    with pytest.raises(DecisionError) as caught:
        client.ask('state', {'c': CHOICE, 'n': NOUL})
    assert caught.value.code == 'format_error'


# --- request checks ----------------------------------------------------------
@pytest.mark.parametrize('state', ['', '   ', {}, [], 3, None])
def test_empty_or_odd_state_is_rejected_before_sending(state):
    server = Server()
    with pytest.raises(DecisionError) as caught:
        davi(server).decide(state, CHOICE)
    assert caught.value.code == 'invalid_request'
    assert not server.requests


def test_option_limit_is_checked_before_sending():
    server = Server()
    many = ChoiceQuestion('pick', {f'o{i}': f'option {i}' for i in range(3)})
    with pytest.raises(DecisionError) as caught:
        davi(server, max_options=2).decide('state', many)
    assert caught.value.code == 'invalid_request'
    assert not server.requests


# --- errors ------------------------------------------------------------------
@pytest.mark.parametrize(
    'reply, code',
    [
        (
            (422, {'error': 'InputTooLong: q: 70000 tokens > 65536'}),
            'invalid_request',
        ),
        ((400, {'error': 'ValueError: bad'}), 'invalid_request'),
        ((401, {'error': 'unauthorized'}), 'credential_missing'),
        ((402, {'error': 'no credit'}), 'provider_error'),
        ((429, {'error': 'slow down'}), 'provider_error'),
        ((500, 'boom'), 'provider_error'),
        ((200, 'not json'), 'format_error'),
        ((200, {'model': 'davi'}), 'format_error'),
        (httpx.ReadTimeout('slow'), 'timeout'),
        (httpx.ConnectError('down'), 'provider_error'),
    ],
)
def test_failures_map_to_error_codes_without_retries(reply, code):
    server = Server(reply)
    with pytest.raises(DecisionError) as caught:
        davi(server).decide('state', CHOICE)
    assert caught.value.code == code
    assert len(server.requests) == 1


def test_server_error_names_only_the_exception_class():
    secret_state = 'PRIVATE-TEXT-IN-STATE'
    server = Server((422, {'error': f'InputTooLong: {secret_state}'}))
    with pytest.raises(DecisionError) as caught:
        davi(server).decide(secret_state, CHOICE)
    assert 'InputTooLong' in caught.value.message
    assert secret_state not in str(caught.value)


def test_jev_needs_a_key_and_sends_it_as_bearer():
    server = Server(ok({'q': {'probabilities': {'a': 0.6, 'b': 0.4}}}))
    jev(server).decide('state', CHOICE)
    assert server.requests[0].headers['authorization'] == f'Bearer {KEY}'
    with pytest.raises(DecisionError) as caught:
        jev(Server(), key=None).decide('state', CHOICE)
    assert caught.value.code == 'credential_missing'


def test_key_is_scrubbed_from_transport_errors():
    server = Server(httpx.ConnectError(f'refused Bearer {KEY}'))
    with pytest.raises(DecisionError) as caught:
        jev(server).decide('state', CHOICE)
    assert KEY not in str(caught.value)


def test_davi_sends_no_authorization_without_a_key():
    server = Server(ok({'q': {'probabilities': {'a': 0.6, 'b': 0.4}}}))
    davi(server).decide('state', CHOICE)
    assert 'authorization' not in server.requests[0].headers


def test_reply_meta_names_provider_mode_and_usage():
    server = Server(
        ok(
            {'q': {'noul': 0.3}},
            usage={'input_tokens': 10, 'output_tokens': 0},
            latency_ms=12.5,
        )
    )
    reply = davi(server, noul_mode='native').ask('state', {'q': NOUL})
    data = reply.to_dict()
    assert data['meta']['provider'] == 'davi'
    assert data['meta']['noul_mode'] == 'native'
    assert data['meta']['server_latency_ms'] == 12.5
    assert data['meta']['usage'] == {'input_tokens': 10, 'output_tokens': 0}
    assert data['answers']['q']['p_true'] == 0.3


# --- settings ----------------------------------------------------------------
def test_settings_use_the_same_key_names_as_routing():
    assert decision_settings.DAVI_KEY_ENVS == SECRET_ENVS['davi_systemone']
    assert decision_settings.JEV_KEY_ENVS == SECRET_ENVS['jev']


def test_build_client_reads_options_in_routing_order():
    env = {
        'DAVI_BASE_URL': 'http://fallback.test',
        'DAVI_ROUTE_DAVI_BASE_URL': '"http://override.test/"',
        'CLAUDE_PLUGIN_OPTION_NOUL_MODE': 'native',
    }
    client = build_client('davi', env)
    assert client.base_url == 'http://override.test'
    assert client.noul_mode == 'native'
    assert isinstance(build_client('jev', {}), JevClient)
    with pytest.raises(ValueError):
        build_client('davi', {})
    with pytest.raises(ValueError):
        build_client('davi', env, noul_mode='boolean')


def test_options_file_fills_only_unset_options(tmp_path):
    path = tmp_path / 'davi-route.json'
    path.write_text(
        json.dumps(
            {'davi_base_url': 'http://file.test', 'noul_mode': 'native'}
        )
    )
    env = {'DAVI_ROUTE_NOUL_MODE': 'choice'}
    merged = with_options_file(env, str(path))
    assert merged['DAVI_ROUTE_DAVI_BASE_URL'] == 'http://file.test'
    assert merged['DAVI_ROUTE_NOUL_MODE'] == 'choice'
    assert env == {'DAVI_ROUTE_NOUL_MODE': 'choice'}
    assert with_options_file({}, str(tmp_path / 'missing.json')) == {}
