# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""The dashboard's HTTP server (standard library only).

    GET  /                          the page
    GET  /static/<file>             its script and style
    GET  /api/state                 questions, recordings, live status
    POST /api/runs                  {"mode": "live", "task_id" | "question"}
                                    {"mode": "replay", "recording_id",
                                     "speed"}
    GET  /api/runs/<id>/events      the run's events (server-sent events)

It binds to 127.0.0.1 by default: a live run spends money.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..policy import TierPolicy
from .runs import LiveRun, ReplayRun
from .tasks import DemoTask

STATIC_DIR = Path(__file__).resolve().parent / 'static'
STATIC_FILES = {
    'index.html': 'text/html; charset=utf-8',
    'app.js': 'text/javascript; charset=utf-8',
    'style.css': 'text/css; charset=utf-8',
    'flow.js': 'text/javascript; charset=utf-8',
    'bench.js': 'text/javascript; charset=utf-8',
    'iso-kit.js': 'text/javascript; charset=utf-8',
    'iso.css': 'text/css; charset=utf-8',
}
MAX_BODY_BYTES = 64_000
MAX_KEPT_RUNS = 40
SSE_WAIT_SECONDS = 15.0
REPLAY_SPEEDS = (1.0, 2.0, 4.0, 8.0)


class BusyError(RuntimeError):
    """A live run is already going."""


class DashboardApp:
    """Questions, recordings and the runs on screen."""

    def __init__(
        self,
        tasks,
        store,
        config=None,
        judges=('davi',),
        policy=None,
        live_problem=None,
        run_kwargs=None,
    ):
        self._tasks = {task.id: task for task in tasks}
        self._order = [task.id for task in tasks]
        self._store = store
        self._config = config
        self._judges = tuple(judges)
        self._policy = policy or TierPolicy()
        self._live_problem = live_problem
        if config is None and not live_problem:
            self._live_problem = '실시간 실행이 꺼져 있습니다 (재생 전용).'
        self._run_kwargs = dict(run_kwargs or {})
        self._runs = {}
        self._live = None
        self._lock = threading.Lock()

    @property
    def live_enabled(self):
        return not self._live_problem

    def state(self):
        with self._lock:
            busy = self._live is not None and not self._live.finished
        policy = self._policy
        return {
            'tasks': [
                self._tasks[task_id].to_dict() for task_id in self._order
            ],
            'recordings': self._store.list(),
            'live': {
                'enabled': self.live_enabled,
                'problem': self._live_problem,
                'busy': busy,
            },
            'judges': list(self._judges),
            'main_model': self._config.main_model if self._config else None,
            'tiers': {
                'low': policy.low,
                'high': policy.high,
                'models': {
                    tier: policy.model_for(tier)
                    for tier in ('cheap', 'mid', 'strong')
                },
            },
            'replay_speeds': list(REPLAY_SPEEDS),
        }

    def start_live(self, task_id=None, question=None):
        if not self.live_enabled:
            raise PermissionError(self._live_problem)
        if task_id:
            if task_id not in self._tasks:
                raise LookupError(f'unknown task {task_id}')
            task = self._tasks[task_id]
        else:
            task = DemoTask.free(question)
        with self._lock:
            if self._live is not None and not self._live.finished:
                raise BusyError('이미 실행 중인 시연이 있습니다.')
            run = LiveRun(
                _new_id(),
                task,
                self._config,
                store=self._store,
                judges=self._judges,
                policy=self._policy,
                **self._run_kwargs,
            )
            self._live = run
            self._keep(run)
        return run.start()

    def start_replay(self, recording_id, speed=1.0):
        recording = self._store.load(recording_id)
        if speed not in REPLAY_SPEEDS:
            raise ValueError(f'speed must be one of {REPLAY_SPEEDS}')
        run = ReplayRun(_new_id(), recording, speed=speed)
        with self._lock:
            self._keep(run)
        return run.start()

    def run(self, run_id):
        with self._lock:
            return self._runs.get(run_id)

    def _keep(self, run):
        self._runs[run.run_id] = run
        finished = [key for key, kept in self._runs.items() if kept.finished]
        for key in finished[: max(0, len(self._runs) - MAX_KEPT_RUNS)]:
            del self._runs[key]


def _new_id():
    return uuid.uuid4().hex[:12]


class DashboardHandler(BaseHTTPRequestHandler):
    """Routes requests to the ``DashboardApp`` of the server."""

    server_version = 'davi-route-dashboard'

    @property
    def app(self):
        return self.server.app

    def log_message(self, format, *args):  # noqa: A002 - base signature
        """Quiet: the terminal stays readable during a demo."""

    def do_GET(self):  # noqa: N802 - http.server naming
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            return self._static('index.html')
        if path.startswith('/static/'):
            return self._static(path[len('/static/') :])
        if path == '/api/state':
            return self._json(200, self.app.state())
        parts = path.strip('/').split('/')
        if len(parts) == 4 and parts[:2] == ['api', 'runs']:
            if parts[3] == 'events':
                return self._events(parts[2])
        return self._json(404, {'error': 'not found'})

    def do_POST(self):  # noqa: N802 - http.server naming
        if self.path.split('?', 1)[0] != '/api/runs':
            return self._json(404, {'error': 'not found'})
        try:
            body = self._body()
            run = self._start(body)
        except BusyError as error:
            return self._json(409, {'error': str(error)})
        except PermissionError as error:
            return self._json(403, {'error': str(error)})
        except (LookupError, ValueError, TypeError) as error:
            return self._json(400, {'error': str(error)})
        return self._json(201, {'run_id': run.run_id})

    def _start(self, body):
        mode = body.get('mode')
        if mode == 'live':
            return self.app.start_live(
                task_id=body.get('task_id'), question=body.get('question')
            )
        if mode == 'replay':
            return self.app.start_replay(
                str(body.get('recording_id') or ''),
                speed=float(body.get('speed') or 1.0),
            )
        raise ValueError('mode must be live or replay')

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError('request too large')
        data = json.loads(self.rfile.read(length) or b'{}')
        if not isinstance(data, dict):
            raise ValueError('a JSON object is expected')
        return data

    def _static(self, name):
        content_type = STATIC_FILES.get(name)
        if content_type is None:
            return self._json(404, {'error': 'not found'})
        body = (STATIC_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
        return None

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
        return None

    def _events(self, run_id):
        run = self.app.run(run_id)
        if run is None:
            return self._json(404, {'error': 'unknown run'})
        after = int(self.headers.get('Last-Event-ID') or 0)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self._stream(run.channel, after)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def _stream(self, channel, after):
        while True:
            events = channel.read(after, timeout=SSE_WAIT_SECONDS)
            for event in events:
                payload = json.dumps(event.to_dict(), ensure_ascii=False)
                self.wfile.write(
                    f'id: {event.seq}\ndata: {payload}\n\n'.encode('utf-8')
                )
                after = event.seq
            if not events:
                if channel.closed:
                    return
                self.wfile.write(b': keepalive\n\n')
            self.wfile.flush()


class DashboardServer(ThreadingHTTPServer):
    """A threading HTTP server that carries the app."""

    daemon_threads = True

    def __init__(self, address, app):
        super().__init__(address, DashboardHandler)
        self._app = app

    @property
    def app(self):
        return self._app

    @property
    def url(self):
        host, port = self.server_address[:2]
        return f'http://{host}:{port}/'


def default_record_dir():
    return os.path.join(os.getcwd(), 'dashboard-recordings')
