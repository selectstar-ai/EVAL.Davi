"""The davi-route dashboard: events, sessions, runs, recordings, server.

Model-free: ``claude -p`` is a fake runner that writes hook log lines, and
the judges are fake verifiers.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from judge_adapter.routing.dashboard.events import EventChannel
from judge_adapter.routing.dashboard.runs import (
    LiveRun,
    ReplayRun,
    summarize,
)
from judge_adapter.routing.dashboard.server import (
    DashboardApp,
    DashboardServer,
)
from judge_adapter.routing.dashboard.sessions import (
    ClaudeSession,
    HookLogTail,
    SessionConfig,
    answer_line,
)
from judge_adapter.routing.dashboard.store import RecordingStore
from judge_adapter.routing.dashboard.tasks import (
    DemoTask,
    load_demo_tasks,
    opus_arm,
    routed_arm,
)
from judge_adapter.routing.verifiers import FakeVerifier

TASK = DemoTask(
    't1', '대한민국의 수도는?', title='수도', answer='서울', grader='contains'
)
ENV = {
    'HOME': '/home/x',
    'PATH': '/bin',
    'DAVI_BASE_URL': 'http://davi.test',
    'ANTHROPIC_API_KEY': 'sk-ant-not-for-claude-p',
}


def hook_line(**record):
    return json.dumps({'event': 'davi-route', **record}) + '\n'


def fake_runner(seen):
    """Plays a session: hook log lines, then the claude -p result."""

    def run(command, env, cwd, timeout):
        seen.append(env)
        routed = env.get('DAVI_ROUTE_ROUTE_MODE') == 'tiers'
        model = 'haiku' if routed else 'opus'
        pre = {'kind': 'pre', 'status': 'ok', 'model_set': model}
        if routed:
            pre.update(
                level=0.1,
                tier='cheap',
                judge='davi',
                latency_s=0.2,
                tier_low=0.48,
                tier_high=0.78,
            )
        with open(env['DAVI_ROUTE_LOG_FILE'], 'a', encoding='utf-8') as log:
            log.write(hook_line(**pre))
            log.write(
                hook_line(
                    kind='post',
                    model_in=model,
                    resolved_model=f'claude-{model}-5-5',
                )
            )
        return {
            'result': '풀이...\n정답: 서울',
            'is_error': False,
            'total_cost_usd': 0.02 if routed else 0.10,
            'wall_s': 3.0,
            'model_usage': {f'claude-{model}-5-5': {'costUSD': 0.01}},
        }

    return run


def config(tmp_path):
    return SessionConfig(
        plugin_dir='plugins/davi-route',
        env=ENV,
        grade_env={},
        work_root=str(tmp_path),
    )


def fake_judges(name):
    defaults = {'davi': 0.9, 'jev': 0.5, 'claude': 0.1}
    return FakeVerifier(default=defaults[name], name=name)


def live_run(tmp_path, store=None, seen=None):
    return LiveRun(
        'r1',
        TASK,
        config(tmp_path),
        store=store,
        judges=('davi', 'jev', 'claude'),
        runner=fake_runner(seen or []),
        verifier_factory=fake_judges,
    )


# --- events -----------------------------------------------------------------
def test_channel_readers_wait_and_see_every_event():
    channel = EventChannel('x')
    got = []
    reader = threading.Thread(
        target=lambda: got.extend(channel.read(0, timeout=5))
    )
    reader.start()
    channel.emit('a', {'n': 1})
    reader.join(5)
    assert [event.type for event in got] == ['a']
    channel.emit('b')
    assert [event.seq for event in channel.read(1, timeout=0)] == [2]
    channel.close()
    assert channel.read(2, timeout=5) == []
    with pytest.raises(RuntimeError):
        channel.emit('c')


def test_hook_log_tail_waits_for_whole_lines(tmp_path):
    path = tmp_path / 'h.jsonl'
    tail = HookLogTail(str(path))
    assert tail.read_new() == []
    path.write_text('{"a": 1}\n{"b"', encoding='utf-8')
    assert tail.read_new() == [{'a': 1}]
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(': 2}\nnot json\n')
    assert tail.read_new() == [{'b': 2}]


# --- tasks and sessions -----------------------------------------------------
def test_demo_tasks(tmp_path):
    path = tmp_path / 't.jsonl'
    path.write_text(
        json.dumps({'id': 'a', 'prompt': 'q?', 'topic': 'T', 'answer': 'x'})
        + '\n'
        + json.dumps({'id': 'b', 'question': 'r?'})
        + '\n',
        encoding='utf-8',
    )
    first, second = load_demo_tasks(str(path))
    assert first.title == 'T' and first.gradable and not second.gradable
    assert 'Agent 도구' in first.prompt() and 'q?' in first.prompt()
    assert DemoTask.free('  hi ').question == 'hi'
    with pytest.raises(ValueError):
        DemoTask.free('   ')
    path.write_text(json.dumps({'id': 'a'}) + '\n', encoding='utf-8')
    with pytest.raises(ValueError):
        load_demo_tasks(str(path))


def test_sessions_never_get_the_anthropic_key(tmp_path):
    routed = ClaudeSession(routed_arm(), config(tmp_path))
    env = routed.child_env('/log', '/state')
    assert 'ANTHROPIC_API_KEY' not in env
    assert env['DAVI_BASE_URL'] == 'http://davi.test'
    assert env['DAVI_ROUTE_ROUTE_MODE'] == 'tiers'
    assert env['DAVI_ROUTE_LOG_FILE'] == '/log'
    opus = ClaudeSession(opus_arm(), config(tmp_path))
    env = opus.child_env('/log', '/state')
    assert 'DAVI_BASE_URL' not in env
    assert env['DAVI_ROUTE_ROUTE_CHEAP_MODEL'] == 'opus'
    command = routed.command(TASK)
    assert command[command.index('--model') + 1] == 'sonnet'
    assert '--plugin-dir' in command and 'WebSearch' in command


def test_answer_line():
    assert answer_line('a\n정답: B\n끝') == '정답: B'
    assert answer_line('a\nb\n') == 'b'
    assert answer_line(None) == ''


# --- runs -------------------------------------------------------------------
def test_live_run_events_summary_and_recording(tmp_path):
    store = RecordingStore(str(tmp_path / 'rec'))
    seen = []
    run = live_run(tmp_path, store, seen)
    run.run()
    events = run.channel.events()
    types = [event.type for event in events]
    assert types[0] == 'run_started' and types[-1] == 'run_done'
    assert types.count('judge') == 3 and types.count('session_done') == 2
    routing = [e.data for e in events if e.type == 'routing']
    assert {data['arm'] for data in routing} == {'routed', 'opus'}
    routed = next(data for data in routing if data['arm'] == 'routed')
    assert routed['tier_label'] == '가벼운 모델'
    judges = {e.data['judge']: e.data for e in events if e.type == 'judge'}
    assert judges['davi']['model'] == 'haiku'  # fake: level = 1 - p
    assert judges['claude']['model'] == 'opus'
    summary = next(e.data for e in events if e.type == 'summary')
    assert summary['routed_model'] == 'haiku'
    assert summary['routed_resolved_model'] == 'claude-haiku-5-5'
    assert summary['saving_usd'] == pytest.approx(0.08)
    assert summary['saving_pct'] == pytest.approx(0.8)
    assert summary['routed_correct'] is True
    assert all('ANTHROPIC_API_KEY' not in env for env in seen)
    recording_id = events[-1].data['recording_id']
    (listed,) = store.list()
    assert listed['id'] == recording_id and listed['routed_model'] == 'haiku'


def test_a_failing_session_still_ends_the_run(tmp_path):
    def broken(command, env, cwd, timeout):
        raise OSError('no claude')

    run = LiveRun(
        'r2',
        TASK,
        config(tmp_path),
        runner=broken,
        verifier_factory=fake_judges,
    )
    run.run()
    done = [e.data for e in run.channel.events() if e.type == 'session_done']
    assert len(done) == 2 and all(data['is_error'] for data in done)
    assert 'OSError' in done[0]['error']
    assert run.finished


def test_replay_keeps_order_and_timing(tmp_path):
    store = RecordingStore(str(tmp_path / 'rec'))
    live = live_run(tmp_path, store)
    live.run()
    recording = store.load(store.list()[0]['id'])
    slept = []
    replay = ReplayRun('p1', recording, speed=4.0, sleep=slept.append)
    replay.run()
    original = [event['type'] for event in recording['events']]
    replayed = [event.type for event in replay.channel.events()]
    assert replayed == original + ['run_done']
    started = replay.channel.events()[0].data
    assert started['mode'] == 'replay' and started['speed'] == 4.0
    last_t = recording['events'][-1]['t']
    assert sum(slept) == pytest.approx(last_t / 4.0, abs=1e-6)
    with pytest.raises(ValueError):
        ReplayRun('p2', recording, speed=0)


def test_summarize_without_costs():
    assert summarize([])['saving_usd'] is None


def test_store_rejects_unsafe_ids(tmp_path):
    store = RecordingStore(str(tmp_path))
    with pytest.raises(KeyError):
        store.load('../etc/passwd')
    with pytest.raises(KeyError):
        store.load('missing')


# --- server -----------------------------------------------------------------
@pytest.fixture
def server(tmp_path):
    store = RecordingStore(str(tmp_path / 'rec'))
    live_run(tmp_path, store).run()
    app = DashboardApp(
        [TASK],
        store,
        config=config(tmp_path),
        judges=('davi',),
        run_kwargs={
            'runner': fake_runner([]),
            'verifier_factory': fake_judges,
        },
    )
    httpd = DashboardServer(('127.0.0.1', 0), app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def request(httpd, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        httpd.url.rstrip('/') + path,
        data=data,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read().decode('utf-8')
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8')


def stream(httpd, run_id):
    status, text = request(httpd, f'/api/runs/{run_id}/events')
    assert status == 200
    return [
        json.loads(line[len('data: ') :])
        for line in text.splitlines()
        if line.startswith('data: ')
    ]


def test_server_state_page_and_static(server):
    status, text = request(server, '/api/state')
    state = json.loads(text)
    assert status == 200 and state['tasks'][0]['id'] == 't1'
    assert state['live']['enabled'] and len(state['recordings']) == 1
    status, text = request(server, '/')
    assert status == 200 and 'Davi 라우팅 라이브' in text
    assert request(server, '/static/app.js')[0] == 200
    assert request(server, '/static/../server.py')[0] == 404
    assert request(server, '/api/nothing')[0] == 404


def test_server_live_and_replay_runs(server):
    status, text = request(
        server, '/api/runs', {'mode': 'live', 'task_id': 't1'}
    )
    assert status == 201
    events = stream(server, json.loads(text)['run_id'])
    assert events[-1]['type'] == 'run_done'
    assert any(event['type'] == 'summary' for event in events)
    recording_id = json.loads(request(server, '/api/state')[1])['recordings'][
        0
    ]['id']
    status, text = request(
        server,
        '/api/runs',
        {
            'mode': 'replay',
            'recording_id': recording_id,
            'speed': 8,
        },
    )
    assert status == 201
    events = stream(server, json.loads(text)['run_id'])
    assert events[0]['data']['mode'] == 'replay'
    bad = request(server, '/api/runs', {'mode': 'live', 'task_id': 'zz'})
    assert bad[0] == 400
    bad = request(server, '/api/runs', {'mode': 'replay', 'speed': 3})
    assert bad[0] == 400


def test_live_runs_can_be_turned_off(tmp_path):
    app = DashboardApp([TASK], RecordingStore(str(tmp_path)))
    assert not app.live_enabled
    with pytest.raises(PermissionError):
        app.start_live(task_id='t1')
