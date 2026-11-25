"""davi-decision-mcp: reference input, the decide tool and the MCP server.

Model-free: the judge is an in-process MockTransport, or for the stdio
test a local HTTP server on 127.0.0.1. The MCP tests are skipped when the
SDK is not installed (the base install, Python 3.9).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from pathlib import Path

import httpx
import pytest

from judge_adapter.decision import DaviClient, DecisionError, JevClient
from judge_adapter.decision_mcp import cli
from judge_adapter.decision_mcp.files import StateFiles
from judge_adapter.decision_mcp.tool import Decider

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'SECRET-MATERIAL-7f3a'
KEY = 'sk-test-secret-0123456789'
CHOICE_ARGS = {
    'type': 'choice',
    'instructions': 'Which document answers the query?',
    'criteria': {'a': 'Doc a', 'b': 'Doc b'},
}
NOUL_ARGS = {
    'type': 'noul',
    'instructions': 'Does the passage answer the query?',
    'criteria': {'true': 'it does', 'false': 'it does not'},
}
CHOICE_REPLY = {'q': {'choice': 'a', 'probabilities': {'a': 0.7, 'b': 0.3}}}
needs_mcp = pytest.mark.skipif(
    find_spec('mcp') is None, reason='the mcp extra is not installed'
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
            return httpx.Response(status, json=body)

        return httpx.MockTransport(handle)

    def body(self, index=0):
        return json.loads(self._requests[index].content)


def ok(answers):
    return 200, {'model': 'davi', 'answers': answers, 'usage': {}}


def decider(server, root, noul_mode='choice', client=None):
    client = client or DaviClient(
        'http://davi.test',
        model='davi-test',
        noul_mode=noul_mode,
        transport=server.transport(),
    )
    return Decider(client, StateFiles([root]))


def refused(files, *paths, state=None):
    with pytest.raises(DecisionError) as caught:
        files.combine(state, list(paths))
    assert caught.value.code == 'invalid_request'
    return caught.value.message


@pytest.fixture
def root(tmp_path):
    base = tmp_path / 'work'
    base.mkdir()
    (base / 'notes.md').write_text('alpha notes\n', encoding='utf-8')
    (base / 'sub').mkdir()
    (base / 'sub' / 'b.txt').write_text('beta 한국어', encoding='utf-8')
    return base


# --- reference input ---------------------------------------------------------
def test_files_follow_the_state_under_numbered_headers(root):
    files = StateFiles([root])
    combined = files.combine('the query', ['notes.md', 'sub/b.txt'])
    assert combined == (
        'the query\n\n[파일 1: notes.md]\nalpha notes\n\n\n'
        '[파일 2: sub/b.txt]\nbeta 한국어'
    )
    assert files.combine('', [str(root / 'notes.md')]).startswith(
        '[파일 1: notes.md]'
    )


def test_paths_outside_the_roots_are_refused(root, tmp_path):
    outside = tmp_path / 'outside.txt'
    outside.write_text(SECRET, encoding='utf-8')
    files = StateFiles([root])
    assert "'..'" in refused(files, '../outside.txt')
    assert "'..'" in refused(files, 'sub/../notes.md')
    assert 'outside' in refused(files, str(outside))


def test_symbolic_links_cannot_escape(root, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'x.txt').write_text(SECRET, encoding='utf-8')
    (root / 'link.txt').symlink_to(outside / 'x.txt')
    (root / 'linkdir').symlink_to(outside)
    (root / 'inner.txt').symlink_to(root / 'notes.md')
    files = StateFiles([root])
    for path in ('link.txt', 'linkdir/x.txt'):
        message = refused(files, path)
        assert 'outside' in message and SECRET not in message
    assert files.combine(None, ['inner.txt']).endswith('alpha notes\n')


@pytest.mark.parametrize(
    'path',
    [
        '.env',
        '.env.local',
        'cert.pem',
        'tls.KEY',
        'id_rsa',
        '.ENV',
        '.git/config',
        'alias.txt',
    ],
)
def test_key_and_git_files_are_refused(root, path):
    (root / '.git').mkdir()
    for name in (
        '.env',
        '.env.local',
        'cert.pem',
        'tls.KEY',
        'id_rsa',
        '.git/config',
    ):
        (root / name).write_text(SECRET, encoding='utf-8')
    (root / 'alias.txt').symlink_to(root / '.env')
    message = refused(StateFiles([root]), path)
    assert 'key' in message and SECRET not in message


def test_missing_binary_and_large_files_are_refused(root):
    (root / 'bin.dat').write_bytes(b'abc\0def')
    (root / 'latin.txt').write_bytes(b'caf\xe9')
    (root / 'big.txt').write_text('x' * (256 * 1024 + 1), encoding='utf-8')
    (root / 'half.txt').write_text('y' * (200 * 1024), encoding='utf-8')
    files = StateFiles([root])
    assert 'not found' in refused(files, 'nope.txt')
    assert 'not found' in refused(files, 'sub')
    assert 'UTF-8' in refused(files, 'bin.dat')
    assert 'UTF-8' in refused(files, 'latin.txt')
    assert '256 KiB' in refused(files, 'big.txt')
    assert 'state with its files' in refused(files, 'half.txt', 'half.txt')


def test_errors_name_the_file_number_never_the_path_or_content(root):
    secret_dir = root / SECRET
    secret_dir.mkdir()
    (secret_dir / 'bin.dat').write_bytes(SECRET.encode() + b'\0')
    message = refused(StateFiles([root]), 'notes.md', f'{SECRET}/bin.dat')
    assert message.startswith('state_files[2]:')
    assert SECRET not in message and str(root) not in message


def test_state_or_files_are_required_and_counted(root):
    files = StateFiles([root])
    assert 'state' in refused(files, state='  ')
    assert 'more than 16' in refused(files, *['notes.md'] * 17)


def test_default_root_is_the_working_directory(root, monkeypatch):
    monkeypatch.chdir(root)
    assert StateFiles().roots == (os.path.realpath(root),)
    with pytest.raises(ValueError):
        StateFiles([root / 'notes.md'])


# --- the decide tool ---------------------------------------------------------
def test_choice_result_has_the_distribution_and_meta(root):
    server = Server(ok(CHOICE_REPLY))
    result = decider(server, root).decide(
        {**CHOICE_ARGS, 'state': 'query', 'state_files': ['notes.md']}
    )
    assert result['type'] == 'choice' and result['value'] == 'a'
    assert result['probabilities'] == {'a': 0.7, 'b': 0.3}
    assert 'p_true' not in result and 'threshold' not in result
    meta = result['meta']
    assert meta['provider'] == 'davi' and meta['model'] == 'davi-test'
    assert 'noul_mode' not in meta and meta['latency_ms'] >= 0
    sent = server.body()
    assert sent['state'].endswith('[파일 1: notes.md]\nalpha notes\n')
    assert sent['questions']['q']['criteria'] == CHOICE_ARGS['criteria']
    assert 'alpha notes' not in json.dumps(result)


@pytest.mark.parametrize(
    'mode, answer, wire_type',
    [
        ('choice', {'probabilities': {'true': 0.6, 'false': 0.4}}, 'choice'),
        ('native', {'noul': 0.6}, 'noul'),
    ],
)
def test_noul_in_both_modes(root, mode, answer, wire_type):
    server = Server(ok({'q': answer}))
    result = decider(server, root, noul_mode=mode).decide(
        {**NOUL_ARGS, 'state': 'passage', 'threshold': 0.6}
    )
    assert result['value'] is True and result['p_true'] == 0.6
    assert result['threshold'] == 0.6 and 'probabilities' not in result
    assert result['meta']['noul_mode'] == mode
    assert server.body()['questions']['q']['type'] == wire_type


@pytest.mark.parametrize(
    'arguments, part',
    [
        ({**CHOICE_ARGS, 'state': 's', 'threshold': 0.5}, 'only for noul'),
        (
            {
                **CHOICE_ARGS,
                'state': 's',
                'criteria': {f'o{i}': 'x' for i in range(33)},
            },
            'at most 32',
        ),
        (
            {**NOUL_ARGS, 'state': 's', 'criteria': {'yes': 'y', 'no': 'n'}},
            'true',
        ),
        ({**NOUL_ARGS, 'state': 's', 'threshold': 1.5}, 'threshold'),
        ({**CHOICE_ARGS, 'state': ' '}, 'state'),
        ({**CHOICE_ARGS, 'type': 'boolean', 'state': 's'}, 'type'),
        (
            {**CHOICE_ARGS, 'state': 's', 'endpoint': 'http://x'},
            'unexpected argument(s): endpoint',
        ),
        ({**CHOICE_ARGS, 'state': 'x' * (64 * 1024)}, '64 KiB'),
    ],
)
def test_bad_arguments_fail_before_sending(root, arguments, part):
    server = Server()
    with pytest.raises(DecisionError) as caught:
        decider(server, root).decide(arguments)
    assert caught.value.code == 'invalid_request'
    assert part in caught.value.message
    assert not server.requests


@pytest.mark.parametrize(
    'reply, code',
    [
        (httpx.ReadTimeout('slow'), 'timeout'),
        (httpx.ConnectError('refused'), 'provider_error'),
        ((500, {'error': 'boom'}), 'provider_error'),
        ((401, {'error': 'no'}), 'credential_missing'),
        (
            (422, {'error': 'InputTooLong: q: 70000 tokens > 65536'}),
            'invalid_request',
        ),
        (
            (200, {'answers': {'q': {'probabilities': {'a': 0.9}}}}),
            'format_error',
        ),
    ],
)
def test_connection_errors_are_errors_not_answers(root, reply, code):
    with pytest.raises(DecisionError) as caught:
        decider(Server(reply), root).decide({**CHOICE_ARGS, 'state': SECRET})
    assert caught.value.code == code
    assert SECRET not in caught.value.message


def test_a_missing_jev_key_is_credential_missing(root):
    server = Server()
    client = JevClient(lambda: None, transport=server.transport())
    with pytest.raises(DecisionError) as caught:
        decider(server, root, client=client).decide(
            {**CHOICE_ARGS, 'state': 's'}
        )
    assert caught.value.code == 'credential_missing'
    assert not server.requests


def test_server_start_settings_follow_the_option_order(root, tmp_path):
    saved = tmp_path / 'davi-route.json'
    saved.write_text(
        json.dumps(
            {
                'decision_judge': 'jev',
                'noul_mode': 'native',
                'decision_allowed_roots': str(root),
            }
        )
    )
    env = {'DAVI_ROUTE_CONFIG': str(saved), 'JEV_API_KEY': KEY}
    from_file = Decider.from_env(env)
    assert from_file.provider == 'jev' and from_file.noul_mode == 'native'
    assert from_file.roots == (os.path.realpath(root),)
    env.update(
        {
            'DAVI_ROUTE_DECISION_JUDGE': 'davi',
            'DAVI_BASE_URL': 'http://davi.test',
            'DAVI_ROUTE_DAVI_MODEL': 'davi-mini',
        }
    )
    from_env = Decider.from_env(env, noul_mode='choice', roots=[tmp_path])
    assert from_env.provider == 'davi' and from_env.model == 'davi-mini'
    assert from_env.noul_mode == 'choice'
    assert from_env.roots == (os.path.realpath(tmp_path),)
    with pytest.raises(ValueError):
        Decider.from_env({'DAVI_ROUTE_CONFIG': str(saved)}, judge='claude')
    with pytest.raises(ValueError):
        Decider.from_env({'DAVI_ROUTE_CONFIG': str(tmp_path / 'none')})


# --- the command -------------------------------------------------------------
def test_the_command_explains_a_missing_or_wrong_sdk():
    old = cli.sdk_problem(version_info=(3, 9, 6), find_version=lambda: None)
    assert 'Python 3.10' in old and 'judge-adapter[mcp]' in old
    assert 'not installed' in cli.sdk_problem(
        version_info=(3, 11, 0), find_version=lambda: None
    )
    assert 'mcp>=2.3,<3' in cli.sdk_problem(
        version_info=(3, 11, 0), find_version=lambda: '1.30.0'
    )
    assert (
        cli.sdk_problem(version_info=(3, 11, 0), find_version=lambda: '2.3.0')
        is None
    )


def test_the_command_exits_2_on_a_python_without_the_sdk(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'sdk_problem', lambda: 'needs Python 3.10')
    with pytest.raises(SystemExit) as caught:
        cli.main([])
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'needs Python 3.10' in captured.err


@needs_mcp
def test_the_command_exits_2_on_a_bad_setting(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv('DAVI_ROUTE_CONFIG', str(tmp_path / 'none.json'))
    for name in (
        'DAVI_BASE_URL',
        'DAVI_ROUTE_DAVI_BASE_URL',
        'CLAUDE_PLUGIN_OPTION_DAVI_BASE_URL',
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as caught:
        cli.main(['--judge', 'davi'])
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'DAVI_BASE_URL' in captured.err


# --- MCP, in process ---------------------------------------------------------
def _run_client(server, calls):
    """initialize, list_tools, then each call -> (client, tools, results)."""
    import anyio
    from mcp import Client

    async def run():
        async with Client(server, mode='legacy') as client:
            tools = await client.list_tools()
            results = [
                await client.call_tool('decide', arguments)
                for arguments in calls
            ]
            return client.protocol_version, client.server_info, tools, results

    return anyio.run(run)


@needs_mcp
def test_mcp_initialize_list_tools_and_call(root):
    from judge_adapter.decision_mcp.server import build_server

    server = Server(ok(CHOICE_REPLY), (500, {'error': 'boom'}))
    version, info, tools, results = _run_client(
        build_server(decider(server, root)),
        [
            {**CHOICE_ARGS, 'state_files': ['notes.md']},
            {**CHOICE_ARGS, 'state': SECRET},
        ],
    )
    assert version and info.name == 'davi-decision'
    (tool,) = tools.tools
    assert tool.name == 'decide'
    assert 'sent to the configured judge server' in tool.description
    assert tool.input_schema['required'] == [
        'type',
        'instructions',
        'criteria',
    ]
    assert tool.input_schema['additionalProperties'] is False
    assert (
        tool.input_schema['properties']['state_files']['anyOf'][0]['maxItems']
        == 16
    )
    assert 'p_true' in tool.output_schema['properties']
    done, failed = results
    assert not done.is_error
    assert done.structured_content['value'] == 'a'
    assert json.loads(done.content[0].text) == done.structured_content
    assert 'alpha notes' not in done.content[0].text
    assert failed.is_error
    assert failed.structured_content['error']['code'] == 'provider_error'
    assert SECRET not in failed.content[0].text


@needs_mcp
@pytest.mark.parametrize(
    'arguments, field',
    [
        ({**CHOICE_ARGS, 'state': SECRET, 'criteria': [SECRET]}, 'criteria'),
        ({**CHOICE_ARGS, 'state': SECRET, 'threshold': SECRET}, 'threshold'),
        ({**CHOICE_ARGS, 'state': [SECRET]}, 'state'),
        ({'state': SECRET, 'criteria': {SECRET: SECRET}}, 'instructions'),
        ({**CHOICE_ARGS, 'state_files': [SECRET] * 17}, 'state_files'),
        ({**CHOICE_ARGS, 'state': SECRET, SECRET: 1}, 'unexpected'),
    ],
)
def test_schema_errors_do_not_echo_the_input(root, arguments, field):
    from judge_adapter.decision_mcp.server import build_server

    server = Server()
    *_, results = _run_client(build_server(decider(server, root)), [arguments])
    (result,) = results
    assert result.is_error
    text = result.content[0].text
    assert field in text
    assert SECRET not in text and 'input_value' not in text
    assert result.structured_content['error']['code'] == 'invalid_request'
    assert not server.requests


# --- MCP over stdio, in a subprocess -----------------------------------------
class _FakeDavi(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 (http.server's name)
        length = int(self.headers['Content-Length'])
        body = json.loads(self.rfile.read(length))
        (question,) = body['questions'].values()
        answer = (
            {'noul': 0.8}
            if question['type'] == 'noul'
            else {'probabilities': {'a': 0.25, 'b': 0.75}}
        )
        data = json.dumps({'model': body['model'], 'answers': {'q': answer}})
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data.encode())

    def log_message(self, *args):
        pass


@needs_mcp
def test_stdio_carries_only_the_protocol_and_stops_cleanly(root, tmp_path):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), _FakeDavi)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    env = {
        'PATH': os.environ.get('PATH', ''),
        'HOME': str(tmp_path),
        'PYTHONPATH': str(ROOT / 'src'),
        'DAVI_ROUTE_CONFIG': str(tmp_path / 'none.json'),
        'DAVI_BASE_URL': f'http://127.0.0.1:{httpd.server_address[1]}',
        'DAVI_ROUTE_NOUL_MODE': 'native',
    }
    messages = [
        {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2025-11-25',
                'capabilities': {},
                'clientInfo': {'name': 'test', 'version': '0'},
            },
        },
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {
            'jsonrpc': '2.0',
            'id': 3,
            'method': 'tools/call',
            'params': {
                'name': 'decide',
                'arguments': {**CHOICE_ARGS, 'state_files': ['notes.md']},
            },
        },
        {
            'jsonrpc': '2.0',
            'id': 4,
            'method': 'tools/call',
            'params': {
                'name': 'decide',
                'arguments': {**NOUL_ARGS, 'state': SECRET},
            },
        },
        {
            'jsonrpc': '2.0',
            'id': 5,
            'method': 'tools/call',
            'params': {
                'name': 'decide',
                'arguments': {**CHOICE_ARGS, 'state': SECRET, 'threshold': 2},
            },
        },
    ]
    process = subprocess.Popen(
        [
            sys.executable,
            '-m',
            'judge_adapter.decision_mcp.cli',
            '--log-level',
            'INFO',
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=root,
        env=env,
    )
    watchdog = threading.Timer(60, process.kill)
    watchdog.start()
    lines = []
    try:
        for message in messages:
            process.stdin.write(json.dumps(message) + '\n')
            process.stdin.flush()
            if 'id' in message:
                lines.append(process.stdout.readline())
        process.stdin.close()
        lines.extend(process.stdout.read().splitlines())
        stderr = process.stderr.read()
        returncode = process.wait(timeout=30)
    finally:
        watchdog.cancel()
        httpd.shutdown()
    assert returncode == 0, stderr[-2000:]
    replies = {}
    stdout = '\n'.join(lines)
    for line in lines:
        message = json.loads(line)
        assert message['jsonrpc'] == '2.0'
        replies[message.get('id')] = message
    assert set(replies) == {1, 2, 3, 4, 5}
    choice = replies[3]['result']['structuredContent']
    assert choice['value'] == 'b' and choice['probabilities']['b'] == 0.75
    noul = replies[4]['result']['structuredContent']
    assert noul['value'] is True and noul['meta']['noul_mode'] == 'native'
    assert replies[5]['result']['isError'] is True
    for text in (stdout, stderr):
        assert SECRET not in text and 'alpha notes' not in text
    assert 'decide choice: ok' in stderr
    assert env['DAVI_BASE_URL'] not in stderr
