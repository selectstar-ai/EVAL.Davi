"""PromptState lock vs prune under REAL separate processes, ordered step by step with file barriers (no repetition,
no sleeps that decide the outcome).

Each child signals named steps by creating files in a shared directory and waits for the steps it depends on; the
code under test is instrumented only at ``fcntl.flock`` / ``os.remove`` / ``os.stat`` (inside that child) to signal
or pause at the exact point of the race. The critical section is marked by an ``O_EXCL`` file: a second process that
enters while the first is inside records an overlap.

- held: a day-old session whose lock is held (and waited on) must not have its lock unlinked by prune, and a
  newcomer must not get the lock while the holder is inside;
- unlinked_while_waiting: a waiter that ends up holding a flock on an unlinked lock file must not proceed
  (inode re-check after acquiring);
- fresh_update: prune that decided "day old" before a concurrent update must not remove the freshly written state
  (expiry re-checked under the lock).
No network, no keys, no real user state: everything lives under pytest's tmp_path."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from judge_adapter import prompt_signal as PS

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform.startswith('win'), reason='fcntl lock: POSIX only')

SESSION = 'race-session'
EXPLICIT = 'Using web search, confirm how many days there are in a week. Answer in one sentence.'

CHILD = r'''
import fcntl, json, os, sys, time
sys.path.insert(0, os.environ['SRC'])
from judge_adapter import prompt_signal as PS
role, root, sig, session = sys.argv[1:5]
real_remove, real_stat, real_open, real_flock = os.remove, os.stat, os.open, fcntl.flock
st = PS.PromptState(root)
state = st.path(session)
lock = state[:-5] + '.lock'
out = {'role': role, 'timeouts': [], 'overlap': False, 'got': None, 'flock_ok': 0}

def signal(name):
    os.close(real_open(os.path.join(sig, name), os.O_CREAT | os.O_WRONLY, 0o600))

def wait(name, timeout=5.0):
    p, end = os.path.join(sig, name), time.monotonic() + timeout
    while True:
        try:
            real_stat(p)
            return True
        except FileNotFoundError:
            if time.monotonic() > end:
                out['timeouts'].append(name)
                return False
            time.sleep(0.002)

def enter():
    try:
        os.close(real_open(os.path.join(sig, 'inside'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        return True
    except FileExistsError:
        out['overlap'] = True            # another process is inside the critical section for the same key
        return False

def leave(owned):
    if owned:
        real_remove(os.path.join(sig, 'inside'))

def hook_flock(first_signal):
    seen = [False]
    def flock(fd, op):
        if not seen[0]:
            seen[0] = True
            signal(first_signal)         # our fd already names the current lock file
        real_flock(fd, op)
        out['flock_ok'] += 1
    fcntl.flock = flock

def lock_and_enter():
    fd = st._lock(session)
    out['got'] = fd is not None
    return fd, (enter() if fd is not None else False)

if role == 'holder':                     # held: holds the lock until the newcomer is done
    fd = st._lock(session)
    out['got'] = fd is not None
    owned = enter()
    ino = os.fstat(fd).st_ino
    signal('h_in')
    wait('n_done')
    try:
        out['lock_kept'] = real_stat(lock).st_ino == ino
    except FileNotFoundError:
        out['lock_kept'] = False
    leave(owned)
    os.close(fd)
elif role == 'waiter':                   # held: waits on the holder's lock file
    wait('h_in')
    hook_flock('w_trying')
    fd, owned = lock_and_enter()
    leave(owned)
    if fd is not None:
        os.close(fd)
elif role == 'pruner':                   # held: prunes while the lock is held and waited on
    wait('w_trying')
    st.prune()
    signal('pruned')
elif role == 'newcomer':                 # held: arrives after the prune
    wait('pruned')
    fd, owned = lock_and_enter()
    leave(owned)
    if fd is not None:
        os.close(fd)
    signal('n_done')
elif role == 'pruner2':                  # unlinked_while_waiting: pauses around its removals
    def remove(p):
        if p == state:
            signal('p_locked')
            wait('w_trying')
            real_remove(p)
        elif p == lock:
            real_remove(p)
            signal('p_unlinked')
            wait('n_in')
        else:
            real_remove(p)
    os.remove = remove
    st.prune()
    signal('pruned')
elif role == 'waiter2':                  # unlinked_while_waiting: opens the old lock file before it is unlinked
    wait('p_locked')
    hook_flock('w_trying')
    fd, owned = lock_and_enter()
    if owned:
        wait('n_in')
    leave(owned)
    if fd is not None:
        os.close(fd)
    signal('w_done')
elif role == 'newcomer2':                # unlinked_while_waiting: locks the new lock file and stays inside
    wait('p_unlinked')
    fd, owned = lock_and_enter()
    signal('n_in')
    if owned:
        wait('w_done')
    leave(owned)
    if fd is not None:
        os.close(fd)
elif role == 'pruner3':                  # fresh_update: pauses right after it saw the day-old mtime
    seen = [False]
    def stat(p, *a, **k):
        r = real_stat(p, *a, **k)
        if p == state and not seen[0]:
            seen[0] = True
            signal('p_statted')
            wait('w_done')
        return r
    os.stat = stat
    st.prune()
    signal('pruned')
elif role == 'writer3':                  # fresh_update: a new prompt updates the state in between
    wait('p_statted')
    out['status'] = st.on_prompt(session, os.environ['EXPLICIT'])[0]
    signal('w_done')
print(json.dumps(out))
'''


def day_old_state(root):
    st = PS.PromptState(str(root))
    assert st.record(SESSION, False)
    old = time.time() - 2 * PS.PRUNE_AGE_SECONDS
    os.utime(st.path(SESSION), (old, old))
    return st


def launch(tmp_path, roles):
    """Start every role at once; the barrier files alone order the steps. -> {role: parsed stdout}."""
    script, root, sig = tmp_path / 'child.py', tmp_path / 'state', tmp_path / 'sig'
    script.write_text(CHILD)
    sig.mkdir()
    env = {**os.environ, 'SRC': str(ROOT / 'src'), 'EXPLICIT': EXPLICIT}
    procs = [subprocess.Popen([sys.executable, str(script), r, str(root), str(sig), SESSION], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env) for r in roles]
    res = {}
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        d = json.loads(out.strip().splitlines()[-1])
        res[d['role']] = d
    return res


def test_prune_never_unlinks_a_held_and_waited_lock(tmp_path):
    st = day_old_state(tmp_path / 'state')
    res = launch(tmp_path, ['holder', 'waiter', 'pruner', 'newcomer'])
    assert not any(r['timeouts'] for r in res.values()), res
    assert res['holder']['got'] is True
    assert not any(r['overlap'] for r in res.values()), res          # (a) never two holders at once
    assert res['newcomer']['got'] is False                            # it waited the 1 s limit, then gave up
    assert res['holder']['lock_kept'] is True, res                    # (b) the held lock file was not unlinked
    assert os.path.exists(st.path(SESSION))                           # a busy key is skipped, state included


def test_waiter_on_an_unlinked_lock_file_never_proceeds(tmp_path):
    day_old_state(tmp_path / 'state')
    res = launch(tmp_path, ['pruner2', 'waiter2', 'newcomer2'])
    assert not any(r['timeouts'] for r in res.values()), res
    assert res['newcomer2']['got'] is True
    assert not any(r['overlap'] for r in res.values()), res          # (a)
    assert res['waiter2']['got'] is False                             # re-check found the replaced file: retried
    assert res['waiter2']['flock_ok'] == 1                            # ... after really holding the unlinked one


def test_prune_keeps_a_state_updated_after_its_first_look(tmp_path):
    st = day_old_state(tmp_path / 'state')
    res = launch(tmp_path, ['pruner3', 'writer3'])
    assert not any(r['timeouts'] for r in res.values()), res
    assert res['writer3']['status'] == 'explicit_set'
    assert st.read(SESSION) == 'explicit'                             # (b) the fresh state survived the pruner
    assert os.path.exists(st.path(SESSION)[:-5] + '.lock')


# --- in-process regressions -----------------------------------------------------------------------------------------
NEUTRAL = 'Tell me about zebra lanterns at the synthetic festival, in detail please.'


def _age(st, session, days=2):
    old = time.time() - days * PS.PRUNE_AGE_SECONDS
    os.utime(st.path(session), (old, old))


def _hold(st, session):
    """Another open file description holding the session's flock (conflicts even within this process)."""
    import fcntl
    os.makedirs(st.root, exist_ok=True)
    fd = os.open(st.path(session)[:-5] + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_record_prunes_other_sessions_without_waiting_on_any_lock(tmp_path):
    st = PS.PromptState(str(tmp_path / 'p'))
    for s in ('A', 'B', 'C'):
        assert st.record(s, False)
        _age(st, s)
    held = _hold(st, 'C')
    try:
        t0 = time.monotonic()
        assert st.on_prompt('A', NEUTRAL)[0] == 'cleared'        # holds A's lock; prune skips A, never waits on C
        assert time.monotonic() - t0 < 0.5
        assert st.read('A') == 'not_explicit'
        assert not os.path.exists(st.path('B')) and not os.path.exists(st.path('B')[:-5] + '.lock')
        assert os.path.exists(st.path('C'))                       # busy key skipped, state and lock kept
        ino = os.fstat(held).st_ino
        assert os.stat(st.path('C')[:-5] + '.lock').st_ino == ino
    finally:
        os.close(held)
    st.prune()
    assert not os.path.exists(st.path('C')) and not os.path.exists(st.path('C')[:-5] + '.lock')


def test_prune_rechecks_age_under_the_lock(tmp_path):
    st = PS.PromptState(str(tmp_path / 'p'))
    assert st.on_prompt('A', EXPLICIT)[0] == 'explicit_set'
    _age(st, 'A')
    real = st._acquire

    def acquire_then_update(lock, blocking):
        fd = real(lock, blocking)
        os.utime(st.path('A'))                                    # updated between the first look and the lock
        return fd
    st._acquire = acquire_then_update
    st.prune()
    assert st.read('A') == 'explicit'


def test_prune_keeps_fresh_state_and_files_stay_0600(tmp_path):
    st = PS.PromptState(str(tmp_path / 'p'))
    assert st.on_prompt('A', EXPLICIT)[0] == 'explicit_set'
    assert st.on_prompt('B', NEUTRAL)[0] == 'cleared'
    before = sorted(os.listdir(st.root))
    st.prune()
    assert sorted(os.listdir(st.root)) == before and len(before) == 4
    for f in before:
        assert os.stat(os.path.join(st.root, f)).st_mode & 0o777 == 0o600
    assert st.read('A') == 'explicit' and st.read('B') == 'not_explicit'


def test_lock_timeout_keeps_the_1s_limit_and_the_failure_behaviour(tmp_path):
    assert PS.PromptState.LOCK_WAIT_SECONDS == 1.0
    st = PS.PromptState(str(tmp_path / 'p'))
    assert st.record('A', False)
    held = _hold(st, 'A')
    try:
        t0 = time.monotonic()
        assert st.on_prompt('A', NEUTRAL) == ('lock_failed', False)
        assert 0.9 <= time.monotonic() - t0 < 3.0
        assert st.read('A') == 'absent'                           # no "not explicit" state is left
        assert st.on_prompt('A', EXPLICIT) == ('lock_failed', True)
        assert st.read('A') == 'explicit'                         # a signal is still recorded
    finally:
        os.close(held)


def test_lock_io_error_fails_safe_and_prune_removes_nothing(tmp_path, monkeypatch):
    st = PS.PromptState(str(tmp_path / 'p'))
    assert st.record('old', False)
    _age(st, 'old')
    real_open = os.open
    monkeypatch.setattr(PS.os, 'open', lambda p, *a, **k: (_ for _ in ()).throw(OSError('io'))
                        if str(p).endswith('.lock') else real_open(p, *a, **k))
    assert st.on_prompt('A', EXPLICIT) == ('lock_failed', True)
    assert st.read('A') == 'explicit'
    st.prune()
    assert os.path.exists(st.path('old'))                         # could not lock it: left alone


def test_without_fcntl_the_failure_path_and_day_old_pruning_are_unchanged(tmp_path, monkeypatch):
    st = PS.PromptState(str(tmp_path / 'p'))
    assert st.record('old', False)
    _age(st, 'old')
    monkeypatch.setitem(sys.modules, 'fcntl', None)               # import fcntl -> ImportError
    assert st.on_prompt('A', NEUTRAL) == ('lock_failed', False)
    assert st.read('A') == 'absent'
    assert st.on_prompt('A', EXPLICIT) == ('lock_failed', True)   # record() pruned the day-old state, as before
    assert st.read('A') == 'explicit' and not os.path.exists(st.path('old'))
    assert not [f for f in os.listdir(st.root) if f.endswith('.lock')]
