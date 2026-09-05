"""One local watcher and port-forwarding process per enabled worktree."""
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

from .config import read_config
from .snapshot import change_stamp
from .state import file_lock, write_json


def worker_alive(record):
    if not record or not record.get('pid') or not record.get('token'):
        return False
    result = subprocess.run(['ps', '-p', str(record['pid']), '-o', 'command='], capture_output=True, text=True)
    return result.returncode == 0 and ('_watch --token ' + record['token']) in result.stdout


def worker_health(store):
    try:
        return json.loads((store.directory / 'worker-status.json').read_text())
    except FileNotFoundError:
        return {}


def stop_worker(store, state):
    record = state.get('worker')
    if not worker_alive(record):
        return
    os.kill(record['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 20
    while worker_alive(record):
        if time.monotonic() > deadline:
            raise RuntimeError('Local watcher is still stopping. Inspect worker.log before retrying.')
        time.sleep(0.1)


def start_worker(store, transport):
    state = store.read()
    if worker_alive(state.get('worker')):
        health = worker_health(store)
        if health.get('error'):
            raise RuntimeError(f'Watcher needs attention: {health["error"]}. Run stop, then up.')
        return
    token = secrets.token_hex(16)
    package_root = str(Path(__file__).parents[1])
    python_path = package_root + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')
    with open(store.directory / 'worker.log', 'a') as log:
        process = subprocess.Popen([sys.executable, '-m', 'worktree_cloud', '--root', str(store.root), '_watch', '--token', token],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                   env={**os.environ, 'PYTHONPATH': python_path})
    state['worker'] = {'pid': process.pid, 'token': token}
    state.pop('stoppedAt', None)
    store.save(state)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        health = worker_health(store)
        if health.get('token') == token:
            if health.get('error'):
                raise RuntimeError(f'Local watcher failed: {health["error"]}. See {store.directory / "worker.log"}')
            if health.get('ready'):
                return
        if process.poll() is not None:
            raise RuntimeError(f'Local watcher exited. See {store.directory / "worker.log"}')
        time.sleep(0.2)
    raise RuntimeError(f'Local forwarding was not ready within 60 seconds. See {store.directory / "worker.log"}')


def reachable(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.2):
            return True
    except OSError:
        return False


def watch(application, token):
    store = application.store
    forwarding = None
    stopped = False
    health = {'token': token, 'ready': False, 'error': None}
    def stop_handler(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        with file_lock(store.directory / 'worker.lock', timeout=1):
            state = store.read()
            ports = state.get('ports', {})
            if ports:
                forwarding = subprocess.Popen(application.transport.forward_args(state['codespace'], ports),
                                              stdin=subprocess.DEVNULL, start_new_session=True)
                deadline = time.monotonic() + 45
                while not all(reachable(port) for port in ports.values()):
                    if forwarding.poll() is not None:
                        raise RuntimeError(f'gh port forwarding exited ({forwarding.returncode})')
                    if time.monotonic() > deadline:
                        raise RuntimeError('Private local port forwarding did not become ready')
                    time.sleep(0.2)
            health.update(ready=True, updatedAt=time.time())
            write_json(store.directory / 'worker-status.json', health)
            previous = None
            while True:
                if forwarding and forwarding.poll() is not None:
                    raise RuntimeError(f'gh port forwarding stopped ({forwarding.returncode}); run up to restore it')
                try:
                    current = change_stamp(store.root, read_config(store.root))
                    if current != previous:
                        exit_code = application.sync()
                        if exit_code == 0:
                            previous = current
                        health.update(error=None if exit_code == 0 else 'Source is changing; waiting for the next snapshot', updatedAt=time.time())
                    else:
                        health.update(error=None, updatedAt=time.time())
                except (OSError, RuntimeError, ValueError) as error:
                    health.update(error=str(error), updatedAt=time.time())
                    print(f'Automatic sync failed: {error}', file=sys.stderr, flush=True)
                write_json(store.directory / 'worker-status.json', health)
                time.sleep(2)
    except KeyboardInterrupt:
        stopped = True
    except (OSError, RuntimeError, ValueError) as error:
        health['error'] = str(error)
        print(str(error), file=sys.stderr, flush=True)
    finally:
        if forwarding and forwarding.poll() is None:
            os.killpg(forwarding.pid, signal.SIGTERM)
            try:
                forwarding.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(forwarding.pid, signal.SIGKILL)
                forwarding.wait()
        health.update(ready=False, stopped=stopped, updatedAt=time.time())
        write_json(store.directory / 'worker-status.json', health)
    return 0 if stopped else 70
